import { clientMeetingErrors, createStartLock } from './client-validation.mjs';

const $ = (sel) => document.querySelector(sel);
const startLock = createStartLock();
const form = $('#meeting-form');
const views = {
  meeting: ['Start a meeting manually', 'Usually your agent does this. Use this page for a one-off meeting.'],
  context: ['Reference context', 'Give Smitline the documents and details behind the discussion.'],
  history: ['Transcripts and handoffs', 'Return to the conversations, decisions, and structured handoffs from your meetings.'],
};
function selectView(view) {
  if (!views[view]) view = 'meeting';
  $('.shell').dataset.view = view;
  $('#page-title').textContent = views[view][0];
  $('#page-description').textContent = views[view][1];
  document.querySelectorAll('[data-view]').forEach(button => {
    if (button.tagName !== 'BUTTON') return;
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
}
document.querySelectorAll('.view-nav button').forEach(button => {
  button.addEventListener('click', () => { location.hash = button.dataset.view; });
});
window.addEventListener('hashchange', () => selectView(location.hash.slice(1)));
selectView(location.hash.slice(1));
let operationBusy = false;
let latestStatus = {};
const ACTIVE_PHASES = new Set([
  'starting', 'opening_meeting', 'joining', 'waiting_for_admission', 'admitted',
  'connecting_audio', 'live',
]);
function meetingBusy(status = {}) {
  return Boolean(status.running) || ACTIVE_PHASES.has(status.phase);
}
let csrf = '';
let savedPasscode = false;
let selectedSession = null;
let selectedAvatar = null;

function payload() {
  const data = new FormData(form);
  return {
    meetingUrl: data.get('meetingUrl')?.trim(),
    objective: data.get('objective')?.trim(),
    passcode: data.get('passcode') || '',
    keepPasscode: savedPasscode && !data.get('passcode'),
    participantName: data.get('participantName')?.trim(),
    meetingInstructions: data.get('meetingInstructions')?.trim(),
    camera: {
      enabled: data.has('cameraEnabled'),
      defaultOn: data.has('cameraDefaultOn'),
      ...(selectedAvatar ? { avatarDataUri: selectedAvatar } : {}),
    },
  };
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Smitline-Token': csrf, ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.error || 'Request failed.'), { result });
  return result;
}

function showErrors(errors = {}) {
  document.querySelectorAll('[data-error]').forEach(node => {
    const message = errors[node.dataset.error] || '';
    node.textContent = message;
    const field = form?.querySelector(`[name="${node.dataset.error}"]`);
    if (field) {
      field.setAttribute('aria-invalid', message ? 'true' : 'false');
      field.setAttribute('aria-describedby', node.id || `${node.dataset.error}-error`);
    }
  });
}

function clientErrors() {
  return clientMeetingErrors(payload());
}

function setBusy(busy, label) {
  operationBusy = busy;
  $('#check-button').disabled = busy;
  $('#start-button').disabled = busy || meetingBusy(latestStatus);
  if (label) $('#start-button').firstChild.textContent = `${label} `;
  else $('#start-button').firstChild.textContent = 'Start Smitline ';
}

function describePhase(phase, health, status = {}) {
  const states = {
    stopped: ['Ready when your meeting is.', 'Complete the setup and run checks.'],
    starting: ['Starting local services…', 'Building the meeting environment and checking connections.'],
    opening_meeting: ['Opening meeting…', 'Preparing the web meeting client.'],
    joining: ['Joining the meeting…', 'Processing the invitation.'],
    waiting_for_admission: ['Waiting to be admitted.', 'The host will see Smitline in the lobby.'],
    admitted: ['Admitted.', 'Connecting meeting audio.'],
    connecting_audio: ['Connecting audio…', 'Preparing the virtual microphone for GPT-Live output.'],
    live: ['Ready to contribute.', 'Listening continuously and waiting for a direct request or a useful factual correction.'],
    authentication_required: ['Sign-in required.', 'Connect a Microsoft account for Teams or a Google account for Meet, or inspect the meeting view.'],
    connecting_account: health?.mode === 'google_account'
      ? ['Sign in to Google.', 'Open meeting view and complete the Google sign-in.']
      : ['Sign in to Microsoft.', 'Open meeting view and complete the Microsoft sign-in.'],
    account_connected: health?.mode === 'google_account'
      ? ['Google account connected.', 'Stop the account browser, then start your meeting.']
      : ['Microsoft account connected.', 'Stop the account browser, then start your meeting.'],
    meeting_ended: ['The meeting has ended.', 'The transcript is available below.'],
    needs_attention: ['The agent needs attention.', status.daemonError || health?.error || 'Open the runtime log for details.'],
    api_error: ['The voice connection failed.', 'Check the API error and restart Smitline.'],
  };
  return states[phase] || ['Working…', 'The current stage is shown above.'];
}

function renderStatus(status) {
  latestStatus = status;
  const health = status.health || {};
  const live = Boolean(status.running);
  const phase = status.phase || 'stopped';
  const [message, detail] = describePhase(phase, health, status);
  $('#connection-label').textContent = live ? 'Local agent connected' : (meetingBusy(status) ? 'Agent starting' : 'Local console ready');
  $('.connection').classList.toggle('live', live);
  $('#phase-label').textContent = phase.replaceAll('_', ' ');
  $('#signal-message').textContent = message;
  $('#signal-detail').textContent = detail;
  $('#signal-stage').className = `signal-stage ${live ? 'active' : ''} ${health.error || phase.includes('error') || phase === 'needs_attention' ? 'error' : ''}`;
  $('#mic-state').textContent = health.microphoneState || '—';
  $('#floor-state').textContent = (health.floorState || '—').replaceAll('_', ' ');
  const cameraState = health.cameraState || (health.cameraEnabled === false ? 'off' : '—');
  $('#camera-state').textContent = health.degradedReason
    ? `${String(cameraState).replaceAll('_', ' ')} (${String(health.degradedReason).replaceAll('_', ' ')})`
    : String(cameraState).replaceAll('_', ' ');
  $('#visual-state').textContent = (health.visualState || '—').replaceAll('_', ' ');
  $('#listening-state').textContent = health.listening === undefined ? '—' : (health.listening ? 'Active' : 'Stopped');
  const seconds = Number(health.usage_seconds || 0);
  $('#session-time').textContent = `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
  $('#stop-button').disabled = !meetingBusy(status);
  $('#start-button').disabled = operationBusy || meetingBusy(status);
  const log = (status.logs || []).map(row => `${row.at.slice(11,19)}  ${row.text}`).join('\n');
  $('#runtime-log').textContent = log || 'No activity yet.';
  renderSessions(status.sessions || []);
}

function handoffStatusLabel(session) {
  const status = session.handoffStatus || 'none';
  if (status === 'ready') return session.partial ? 'Handoff ready (partial)' : 'Handoff ready';
  if (status === 'pending') return 'Handoff pending';
  if (status === 'failed') return 'Handoff incomplete';
  return session.hasTranscript ? 'Transcript only' : 'Meeting session';
}

function renderSessions(sessions) {
  const list = $('#session-list');
  if (!sessions.length) { list.innerHTML = '<p class="empty">Completed meeting transcripts and handoffs will appear here.</p>'; return; }
  list.replaceChildren(...sessions.map(session => {
    const button = document.createElement('button');
    button.className = `session-card ${selectedSession === session.id ? 'selected' : ''}`;
    button.type = 'button';
    const title = document.createElement('b'); title.textContent = handoffStatusLabel(session);
    const time = document.createElement('span'); time.textContent = new Date(session.updatedAt).toLocaleString();
    const status = document.createElement('em');
    status.textContent = session.endReason ? `Ended: ${session.endReason}` : session.id;
    button.append(title, time, status);
    button.addEventListener('click', () => openTranscript(session.id, button));
    return button;
  }));
}

function formatSize(value) {
  return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function renderPendingFiles() {
  const files = [...$('#context-files').files];
  $('#pending-files').replaceChildren(...files.map(file => {
    const pill = document.createElement('span');
    pill.className = 'file-pill';
    pill.textContent = `${file.name} · ${formatSize(file.size)}`;
    return pill;
  }));
}

function renderContext(context) {
  const count = context.sources.length;
  $('#context-count').textContent = count ? `${count} source${count === 1 ? '' : 's'} · ${context.totalCharacters.toLocaleString()} characters` : 'No sources';
  $('#clear-context-button').disabled = count === 0;
  const container = $('#context-sources');
  if (!count) {
    container.innerHTML = '<p class="empty">No private sources are available to the agent.</p>';
    return;
  }
  container.replaceChildren(...context.sources.map(source => {
    const row = document.createElement('div'); row.className = 'source-row';
    const name = document.createElement('b'); name.textContent = source.name;
    const detail = document.createElement('span'); detail.textContent = `${source.kind} · ${source.characters.toLocaleString()} chars`;
    row.append(name, detail);
    return row;
  }));
}

async function filePayload(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768));
  }
  return { name: file.name, type: file.type, data: btoa(binary) };
}

async function addPendingContext(showSuccess = true) {
  const text = $('#context-text').value.trim();
  const files = [...$('#context-files').files];
  if (!text && !files.length) return true;
  if (files.some(file => file.size > 8 * 1024 * 1024)) throw new Error('Each context document must be 8 MB or smaller.');
  if (files.reduce((total, file) => total + file.size, 0) > 12 * 1024 * 1024) throw new Error('Add no more than 12 MB of documents at once.');
  $('#add-context-button').disabled = true;
  $('#add-context-button').textContent = 'Extracting…';
  try {
    const result = await request('/api/context/add', {
      method: 'POST',
      body: JSON.stringify({ text, files: await Promise.all(files.map(filePayload)) }),
    });
    renderContext(result);
    $('#context-text').value = '';
    $('#context-files').value = '';
    renderPendingFiles();
    if (showSuccess) announce('Private meeting context saved locally.');
    return true;
  } finally {
    $('#add-context-button').disabled = false;
    $('#add-context-button').textContent = 'Add context';
  }
}

async function openTranscript(id, button) {
  try {
    const result = await request(`/api/sessions/${encodeURIComponent(id)}`);
    selectedSession = id;
    document.querySelectorAll('.session-card').forEach(node => node.classList.remove('selected'));
    button.classList.add('selected');
    $('#transcript-view h3').textContent = new Date(id.slice(0, 15).replace(/(\d{8})T(\d{6})Z/, '$1T$2Z')).toString() === 'Invalid Date' ? id : id;
    const status = result.handoffStatus || 'none';
    const statusNode = $('#handoff-status');
    statusNode.hidden = false;
    statusNode.textContent = status === 'ready'
      ? (result.partial ? 'Structured handoff is ready. This record is marked partial.' : 'Structured handoff is ready.')
      : status === 'pending'
        ? 'Handoff is stored locally and still being finalized.'
        : status === 'failed'
          ? 'Handoff finalization did not complete. The local handoff is kept.'
          : 'No structured handoff is available yet.';
    $('#transcript-body').textContent = (result.transcript || '').trim() || 'This meeting has no transcript content.';
    const actions = $('#handoff-actions');
    const handoffView = $('#handoff-view');
    if (result.handoff) {
      actions.hidden = false;
      handoffView.hidden = false;
      $('#handoff-body').textContent = JSON.stringify(result.handoff, null, 2);
      $('#download-handoff').onclick = () => {
        const blob = new Blob([JSON.stringify(result.handoff, null, 2)], { type: 'application/json' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = `${result.handoffId || id}-handoff.json`;
        link.click();
        URL.revokeObjectURL(link.href);
      };
    } else {
      actions.hidden = true;
      handoffView.hidden = true;
    }
  } catch (error) { announce(error.message, true); }
}

function announce(text, failure = false) {
  const notice = $('#notice');
  notice.hidden = !text;
  notice.textContent = text;
  notice.classList.toggle('failure', failure);
}

async function runChecks() {
  showErrors();
  const box = $('#preflight-results'); box.className = 'preflight';
  const local = clientErrors();
  if (Object.keys(local).length) {
    showErrors(local);
    box.className = 'preflight failed';
    box.querySelector('p').textContent = Object.values(local).join(' ');
    announce('Fill the required fields marked below.', true);
    const first = form.querySelector(`[name="${Object.keys(local)[0]}"]`);
    first?.focus();
    return false;
  }
  setBusy(true, 'Checking…');
  box.querySelector('p').textContent = 'Checking local services and meeting configuration…';
  try {
    await addPendingContext(false);
    const result = await request('/api/preflight', { method: 'POST', body: JSON.stringify(payload()) });
    showErrors(result.errors);
    box.className = `preflight ${result.ready ? 'ready' : 'failed'}`;
    box.querySelector('p').textContent = result.ready ? 'Ready to join. Your OpenAI key, Docker, and meeting settings passed.' : Object.values(result.errors).join(' ');
    if (result.ready) announce('');
    else announce(Object.values(result.errors)[0] || 'Checks failed.', true);
    return result.ready;
  } catch (error) { box.className = 'preflight failed'; box.querySelector('p').textContent = error.message; announce(error.message, true); return false; }
  finally { setBusy(false); }
}

async function startColleague(event) {
  event?.preventDefault();
  if (!startLock.begin()) return;
  announce('');
  try {
    if (!await runChecks()) return;
    setBusy(true, 'Starting…');
    await request('/api/start', { method: 'POST', body: JSON.stringify(payload()) });
    savedPasscode = savedPasscode || Boolean($('#passcode').value);
    $('#passcode').value = '';
    announce('Smitline is starting. Admit it when it reaches the meeting lobby. It is listed on the Meetings page with its result.');
    await refresh();
  } catch (error) { showErrors(error.result?.errors); announce(error.message, true); }
  finally {
    startLock.end();
    setBusy(false);
  }
}

form.addEventListener('submit', startColleague);
$('#start-button').addEventListener('click', startColleague);
$('#check-button').addEventListener('click', runChecks);
const FIELD_KEYS = { 'meeting-url': 'meetingUrl', 'meeting-objective': 'objective', 'participant-name': 'participantName' };
Object.keys(FIELD_KEYS).forEach((id) => {
  $(`#${id}`)?.addEventListener('input', () => {
    const key = FIELD_KEYS[id];
    const node = document.querySelector(`[data-error="${key}"]`);
    if (node?.textContent) {
      const next = clientErrors();
      node.textContent = next[key] || '';
      $(`#${id}`)?.setAttribute('aria-invalid', next[key] ? 'true' : 'false');
    }
  });
});
$('#context-files').addEventListener('change', renderPendingFiles);
$('#add-context-button').addEventListener('click', async () => {
  try { await addPendingContext(); } catch (error) { announce(error.message, true); }
});
$('#clear-context-button').addEventListener('click', async () => {
  if (!window.confirm('Clear all saved meeting context from this computer?')) return;
  try {
    renderContext(await request('/api/context/clear', { method: 'POST', body: '{}' }));
    announce('Saved meeting context cleared.');
  } catch (error) { announce(error.message, true); }
});
$('#stop-button').addEventListener('click', async () => {
  $('#stop-button').disabled = true; announce('Stopping Smitline…');
  try { await request('/api/stop', { method: 'POST', body: '{}' }); announce('Smitline stopped.'); await refresh(); }
  catch (error) { announce(error.message, true); }
});

async function refresh() {
  try { renderStatus(await request('/api/status')); } catch { $('.connection').classList.remove('live'); $('#connection-label').textContent = 'Console disconnected'; }
}

async function init() {
  try {
    const data = await request('/api/bootstrap'); csrf = data.token;
    const settings = data.settings;
    renderContext(data.context);
    $('#meeting-url').value = settings.meetingUrl;
    updatePlatform();
    $('#participant-name').value = settings.participantName;
    $('#meeting-instructions').value = settings.meetingInstructions;
    savedPasscode = settings.hasPasscode;
    $('#passcode-hint').textContent = savedPasscode ? 'A passcode is saved. Leave blank to keep it.' : 'Optional when the invitation URL includes access credentials.';
    renderStatus(data.status);
    setInterval(refresh, 2000);
  } catch (error) { announce(`Control panel failed to initialize: ${error.message}`, true); }
}

init();

let previewApi = null;
async function loadPreview() {
  if (previewApi) return previewApi;
  try {
    previewApi = await import('./visual-preview.mjs');
  } catch {
    previewApi = { drawPresencePreview() {}, async readAvatarFile() { throw new Error('Camera preview is unavailable.'); } };
  }
  return previewApi;
}
async function paintPreview() {
  const canvas = $('#camera-preview');
  if (!canvas) return;
  const preview = await loadPreview();
  preview.drawPresencePreview(canvas, { visualState: $('#camera-preview-state')?.value || 'listening' });
}
paintPreview();
$('#camera-preview-state')?.addEventListener('change', paintPreview);
$('#camera-avatar')?.addEventListener('change', async event => {
  const file = event.target.files?.[0];
  const errorNode = document.querySelector('[data-error="camera"]');
  try {
    const preview = await loadPreview();
    selectedAvatar = file ? await preview.readAvatarFile(file) : null;
    if (errorNode) errorNode.textContent = '';
    $('#remove-avatar-button').hidden = !selectedAvatar;
  } catch (error) {
    selectedAvatar = null;
    event.target.value = '';
    if (errorNode) errorNode.textContent = error.message;
    $('#remove-avatar-button').hidden = true;
  }
});
$('#remove-avatar-button')?.addEventListener('click', () => {
  selectedAvatar = null;
  $('#camera-avatar').value = '';
  $('#remove-avatar-button').hidden = true;
  const errorNode = document.querySelector('[data-error="camera"]');
  if (errorNode) errorNode.textContent = '';
});

function updatePlatform() {
  let platform = null;
  try {
    const host = new URL($('#meeting-url').value).hostname;
    if (['teams.microsoft.com', 'teams.live.com'].includes(host)) platform = 'Teams';
    else if (/^(?:[a-z0-9-]+\.)?zoom\.us$/.test(host)) platform = 'Zoom';
    else if (host === 'meet.google.com') platform = 'Meet';
  } catch {}
  $('#platform-badge').textContent = platform || 'Zoom / Teams / Meet';
  $('#teams-account').hidden = platform !== 'Teams';
  $('#google-account').hidden = platform !== 'Meet';
  if (platform === 'Teams') refreshAccount('teams');
  if (platform === 'Meet') refreshAccount('google');
}
async function refreshAccount(kind = 'teams') {
  const stateId = kind === 'google' ? 'google-account-state' : 'account-state';
  try {
    const account = await request(`/api/platforms/${kind}/status`);
    const provider = kind === 'google' ? 'Google' : 'Microsoft';
    $(`#${stateId}`).textContent = account.connected
      ? `Connected locally. ${provider} may request sign-in again if the session expires.`
      : 'Not connected. Guest entry will be attempted first.';
  } catch { $(`#${stateId}`).textContent = 'Could not check account state.'; }
}
$('#meeting-url').addEventListener('input', updatePlatform);
for (const [kind, noun] of [['teams', 'Microsoft'], ['google', 'Google']]) {
  for (const action of ['connect', 'disconnect']) {
    $(`#${action}-${kind}`)?.addEventListener('click', async event => {
      event.target.disabled = true;
      try {
        await request(`/api/platforms/${kind}/${action}`, { method: 'POST', body: '{}' });
        announce(action === 'connect'
          ? 'Preparing the account browser. Open meeting view to sign in.'
          : `${noun} profile removed from this computer.`);
        await refreshAccount(kind); await refresh();
      } catch (error) { announce(error.message, true); }
      finally { event.target.disabled = false; }
    });
  }
}
