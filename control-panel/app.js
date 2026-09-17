import { drawPresencePreview, readAvatarFile } from './visual-preview.mjs';
const $ = (sel) => document.querySelector(sel);
const form = $('#meeting-form');
const views = {
  meeting: ['New meeting', 'Set up your colleague, then invite it into the conversation.'],
  context: ['Reference context', 'Give your colleague the documents and details behind the discussion.'],
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
    passcode: data.get('passcode') || '',
    keepPasscode: savedPasscode && !data.get('passcode'),
    participantName: data.get('participantName')?.trim(),
    model: data.get('model'),
    workspace: data.get('workspace')?.trim(),
    meetingInstructions: data.get('meetingInstructions')?.trim(),
    tools: {
      webSearch: data.has('webSearch'), codex: data.has('codex'),
      charts: data.has('charts'),
    },
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
    headers: { 'Content-Type': 'application/json', 'X-Colleague-Token': csrf, ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.error || 'Request failed.'), { result });
  return result;
}

function showErrors(errors = {}) {
  document.querySelectorAll('[data-error]').forEach(node => { node.textContent = errors[node.dataset.error] || ''; });
}

function setBusy(busy, label) {
  operationBusy = busy;
  $('#check-button').disabled = busy;
  $('#start-button').disabled = busy || meetingBusy(latestStatus);
  if (label) $('#start-button').firstChild.textContent = `${label} `;
  else $('#start-button').firstChild.textContent = 'Start colleague ';
}

function describePhase(phase, health, status = {}) {
  if ((status.pendingApprovals || []).length && (status.running || phase === 'live')) {
    return ['Waiting for approval.', 'A workspace action is paused until you approve or deny it.'];
  }
  const states = {
    stopped: ['Ready when your meeting is.', 'Complete the setup and run checks.'],
    starting: ['Starting local services…', 'Building the meeting environment and checking connections.'],
    opening_meeting: ['Opening meeting…', 'Preparing the web meeting client.'],
    joining: ['Joining the meeting…', 'Processing the invitation.'],
    waiting_for_admission: ['Waiting to be admitted.', 'The host will see Colleague AI in the lobby.'],
    admitted: ['Admitted.', 'Connecting meeting audio.'],
    connecting_audio: ['Connecting audio…', 'Preparing the virtual microphone for GPT-Live output.'],
    live: ['Ready to contribute.', 'Listening continuously and waiting for a direct request or a useful factual correction.'],
    authentication_required: ['Sign-in required.', 'Connect a Microsoft account for Teams, or inspect the meeting view.'],
    connecting_account: ['Sign in to Microsoft.', 'Open meeting view and complete the Microsoft sign-in.'],
    account_connected: ['Microsoft account connected.', 'Stop the account browser, then start your meeting.'],
    meeting_ended: ['The meeting has ended.', 'The transcript is available below.'],
    needs_attention: ['The agent needs attention.', status.daemonError || health?.error || 'Open the runtime log for details.'],
    api_error: ['The voice connection failed.', 'Check the API error and restart the colleague.'],
  };
  const [message, detail] = states[phase] || ['Working…', 'The current stage is shown above.'];
  const continuity = status.continuity || health?.codex?.continuity;
  if (continuity === 'context') {
    return [message, detail + ' Codex is using context continuity, not the originating thread.'];
  }
  if (continuity === 'exact') {
    return [message, detail + ' Codex is resuming the originating session.'];
  }
  return [message, detail];
}

function renderApprovals(pending, meetingId) {
  const panel = $('#approvals-panel');
  const list = $('#approval-list');
  if (!panel || !list) return;
  if (!pending.length || !meetingId) {
    panel.hidden = true;
    list.replaceChildren();
    return;
  }
  panel.hidden = false;
  list.replaceChildren(...pending.map((item) => {
    const card = document.createElement('li');
    card.className = 'approval-card';
    const title = document.createElement('b');
    title.textContent = item.category || item.permission || 'action';
    const summary = document.createElement('p');
    summary.textContent = item.summary || 'Requested action needs approval.';
    const meta = document.createElement('div');
    meta.className = 'approval-meta';
    const scope = document.createElement('span');
    const keys = Object.keys(item.scope || {});
    scope.textContent = keys.length ? keys.map((key) => `${key}: ${item.scope[key]}`).join(' · ') : 'meeting scope';
    const expiry = document.createElement('span');
    expiry.textContent = item.expiresAt ? `expires ${item.expiresAt}` : '';
    meta.append(scope, expiry);
    const actions = document.createElement('div');
    actions.className = 'approval-actions';
    for (const [decision, label] of [['approved', 'Approve'], ['denied', 'Deny']]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = decision === 'denied' ? 'button danger compact' : 'button primary compact';
      button.textContent = label;
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          await request(`/api/meetings/${encodeURIComponent(meetingId)}/approvals/${encodeURIComponent(item.id)}/decision`, {
            method: 'POST',
            body: JSON.stringify({ decision }),
          });
          await refresh();
        } catch (error) {
          announce(error.message, true);
          button.disabled = false;
        }
      });
      actions.append(button);
    }
    card.append(title, summary, meta, actions);
    return card;
  }));
}

function renderWorkspace(artifacts, meetingId) {
  const panel = $('#workspace-panel');
  const list = $('#workspace-list');
  if (!panel || !list) return;
  const items = artifacts || [];
  if (!items.length || !meetingId) {
    panel.hidden = true;
    list.replaceChildren();
    return;
  }
  panel.hidden = false;
  list.replaceChildren(...items.map((item) => {
    const card = document.createElement('li');
    card.className = 'workspace-card';
    const title = document.createElement('b');
    title.textContent = item.kind || 'artifact';
    const summary = document.createElement('p');
    summary.textContent = item.description || item.summary || 'Workspace artifact';
    const meta = document.createElement('div');
    meta.className = 'approval-meta';
    const size = document.createElement('span');
    size.textContent = item.bytes != null ? `${item.bytes} bytes` : '';
    const files = document.createElement('span');
    const changed = (item.changedFiles || []).map((file) => file.path || file).filter(Boolean);
    files.textContent = changed.length ? changed.join(', ') : (item.status || '');
    meta.append(size, files);
    const link = document.createElement('button');
    link.type = 'button';
    link.className = 'button ghost compact';
    link.textContent = 'Download';
    link.addEventListener('click', async () => {
      try {
        const response = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/artifacts/${encodeURIComponent(item.id)}/content`, {
          headers: { 'X-Colleague-Token': csrf },
        });
        if (!response.ok) throw new Error('Download failed.');
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = item.id || 'artifact';
        anchor.click();
        URL.revokeObjectURL(url);
      } catch (error) {
        announce(error.message, true);
      }
    });
    card.append(title, summary, meta, link);
    return card;
  }));
}

function renderGit(operations) {
  const panel = $('#git-panel');
  const list = $('#git-list');
  if (!panel || !list) return;
  const items = operations || [];
  if (!items.length) {
    panel.hidden = true;
    list.replaceChildren();
    return;
  }
  panel.hidden = false;
  list.replaceChildren(...items.map((item) => {
    const card = document.createElement('li');
    card.className = 'workspace-card';
    const title = document.createElement('b');
    title.textContent = item.kind || 'git';
    const summary = document.createElement('p');
    const result = item.result || {};
    summary.textContent = result.summary || 'Waiting for a separate approval.';
    const meta = document.createElement('div');
    meta.className = 'approval-meta';
    const status = document.createElement('span');
    status.textContent = item.status || 'requested';
    const detail = document.createElement('span');
    detail.textContent = result.commitSha || item.approvalId || '';
    meta.append(status, detail);
    card.append(title, summary, meta);
    return card;
  }));
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
  const pending = status.pendingApprovals || [];
  const waiting = pending.length > 0;
  $('#signal-stage').className = `signal-stage ${live ? 'active' : ''} ${waiting ? 'waiting' : (health.error || phase.includes('error') || phase === 'needs_attention' ? 'error' : '')}`;
  $('#mic-state').textContent = health.microphoneState || '—';
  $('#floor-state').textContent = (health.floorState || '—').replaceAll('_', ' ');
  const cameraState = health.cameraState || (health.cameraEnabled === false ? 'off' : '—');
  $('#camera-state').textContent = health.degradedReason
    ? `${String(cameraState).replaceAll('_', ' ')} (${String(health.degradedReason).replaceAll('_', ' ')})`
    : String(cameraState).replaceAll('_', ' ');
  $('#visual-state').textContent = (health.visualState || '—').replaceAll('_', ' ');
  $('#listening-state').textContent = health.listening === undefined ? '—' : (health.listening ? 'Active' : 'Stopped');
  $('#tool-state').textContent = health.backend_status || '—';
  const continuity = status.continuity || health.codex?.continuity;
  $('#continuity-state').textContent = continuity === 'exact'
    ? 'Originating thread'
    : continuity === 'context'
      ? 'Context only'
      : '—';
  const seconds = Number(health.usage_seconds || 0);
  $('#session-time').textContent = `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
  renderApprovals(pending, status.meetingId);
  renderWorkspace(status.workspaceArtifacts || [], status.meetingId);
  renderGit(status.gitOperations || []);
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
  if (status === 'failed') return 'Handoff failed — retry available';
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
        ? 'Handoff is stored locally and still pending Codex append or daemon release.'
        : status === 'failed'
          ? 'Codex append failed. The local handoff is kept; retry without releasing the lease.'
          : 'No structured handoff is available yet.';
    $('#transcript-body').textContent = (result.transcript || '').trim() || 'This meeting has no transcript content.';
    const actions = $('#handoff-actions');
    const handoffView = $('#handoff-view');
    const retry = $('#retry-handoff');
    if (result.handoff) {
      actions.hidden = false;
      handoffView.hidden = false;
      $('#handoff-body').textContent = JSON.stringify(result.handoff, null, 2);
      retry.hidden = status !== 'failed';
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          await request(`/api/sessions/${encodeURIComponent(id)}/retry`, { method: 'POST', body: '{}' });
          announce('Codex append retry succeeded.');
          await openTranscript(id, button);
          await refresh();
        } catch (error) {
          announce(error.message, true);
          await openTranscript(id, button);
        } finally {
          retry.disabled = false;
        }
      };
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
      retry.hidden = true;
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
  showErrors(); setBusy(true, 'Checking…');
  const box = $('#preflight-results'); box.className = 'preflight';
  box.querySelector('p').textContent = 'Checking local services and meeting configuration…';
  try {
    await addPendingContext(false);
    const result = await request('/api/preflight', { method: 'POST', body: JSON.stringify(payload()) });
    showErrors(result.errors);
    box.className = `preflight ${result.ready ? 'ready' : 'failed'}`;
    box.querySelector('p').textContent = result.ready ? 'Ready to join. Credentials, Docker, Codex, and meeting settings passed.' : Object.values(result.errors).join(' ');
    return result.ready;
  } catch (error) { box.className = 'preflight failed'; box.querySelector('p').textContent = error.message; return false; }
  finally { setBusy(false); }
}

form.addEventListener('submit', async event => {
  event.preventDefault(); announce('');
  if (!await runChecks()) return;
  setBusy(true, 'Starting…');
  try {
    await request('/api/start', { method: 'POST', body: JSON.stringify(payload()) });
    savedPasscode = savedPasscode || Boolean($('#passcode').value);
    $('#passcode').value = '';
    announce('Colleague AI is starting. Admit it when it reaches the meeting lobby.');
    await refresh();
  } catch (error) { showErrors(error.result?.errors); announce(error.message, true); }
  finally { setBusy(false); }
});

$('#check-button').addEventListener('click', runChecks);
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
  $('#stop-button').disabled = true; announce('Stopping Colleague AI…');
  try { await request('/api/stop', { method: 'POST', body: '{}' }); announce('Colleague AI stopped.'); await refresh(); }
  catch (error) { announce(error.message, true); }
});

form.elements.codex.addEventListener('change', () => {
  const enabled = form.elements.codex.checked;
  $('#workspace-field').hidden = !enabled;
  if (!enabled) form.elements.charts.checked = false;
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
    $('#workspace').value = settings.workspace;
    $('#meeting-instructions').value = settings.meetingInstructions;
    $('#model').replaceChildren(...data.models.map(model => Object.assign(document.createElement('option'), { value: model, textContent: model })));
    $('#model').value = settings.model;
    form.elements.webSearch.checked = settings.tools.webSearch;
    form.elements.codex.checked = settings.tools.codex;
    form.elements.charts.checked = settings.tools.charts;
    savedPasscode = settings.hasPasscode;
    $('#passcode-hint').textContent = savedPasscode ? 'A passcode is saved. Leave blank to keep it.' : 'Optional when the invitation URL includes access credentials.';
    $('#workspace-field').hidden = !settings.tools.codex;
    renderStatus(data.status);
    setInterval(refresh, 2000);
  } catch (error) { announce(`Control panel failed to initialize: ${error.message}`, true); }
}

init();

function paintPreview() {
  const canvas = $('#camera-preview');
  if (!canvas) return;
  drawPresencePreview(canvas, { visualState: $('#camera-preview-state')?.value || 'listening' });
}
paintPreview();
$('#camera-preview-state')?.addEventListener('change', paintPreview);
$('#camera-avatar')?.addEventListener('change', async event => {
  const file = event.target.files?.[0];
  const errorNode = document.querySelector('[data-error="camera"]');
  try {
    selectedAvatar = file ? await readAvatarFile(file) : null;
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
  } catch {}
  $('#platform-badge').textContent = platform || 'Zoom / Teams';
  $('#teams-account').hidden = platform !== 'Teams';
  if (platform === 'Teams') refreshAccount();
}
async function refreshAccount() {
  try {
    const account = await request('/api/platforms/teams/status');
    $('#account-state').textContent = account.connected ? 'Connected locally. Microsoft may request sign-in again if the session expires.' : 'Not connected. Guest entry will be attempted first.';
  } catch { $('#account-state').textContent = 'Could not check account state.'; }
}
$('#meeting-url').addEventListener('input', updatePlatform);
for (const action of ['connect', 'disconnect']) {
  $(`#${action}-teams`).addEventListener('click', async event => {
    event.target.disabled = true;
    try {
      await request(`/api/platforms/teams/${action}`, { method: 'POST', body: '{}' });
      announce(action === 'connect' ? 'Preparing the account browser. Open meeting view to sign in.' : 'Microsoft profile removed from this computer.');
      await refreshAccount(); await refresh();
    } catch (error) { announce(error.message, true); }
    finally { event.target.disabled = false; }
  });
}
