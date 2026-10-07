// The Account page: the setup checklist, and replacing or removing each key and setting.
// The server never returns a key; a saved one shows as "Saved" and at most its known prefix.
const $ = (id) => document.getElementById(id);
const state = { token: '', fields: [], status: null, open: null, mode: null, notes: new Map(), busy: false };

// Which checklist line tells whether a saved field works.
const CHECK_FOR = {
  OPENAI_API_KEY: 'openai_key', SMITLINE_OWNER_NAME: 'owner_name', SMITLINE_OWNER_PHONE: 'owner_phone',
  SMITLINE_CALLER_ID: 'caller_id', SIGNALWIRE_FROM_NUMBER: 'caller_id', TWILIO_FROM_NUMBER: 'caller_id',
  SIGNALWIRE_SPACE: 'phone_account', SIGNALWIRE_PROJECT_ID: 'phone_account', SIGNALWIRE_API_TOKEN: 'phone_account',
  SIGNALWIRE_SIGNING_KEY: 'phone_account', TWILIO_ACCOUNT_SID: 'phone_account', TWILIO_AUTH_TOKEN: 'phone_account',
  OPENAI_PROJECT_ID: 'phone_audio', OPENAI_WEBHOOK_SECRET: 'phone_audio',
};
const GROUP_TITLES = { Required: 'The basics' };
const CHECK_GROUPS = [['core', 'Basics'], ['phone', 'Phone calls (optional)'], ['meetings', 'Meetings (optional)']];

function element(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

function sentence(text) {
  const value = String(text || '').trim();
  if (!value) return '';
  const first = value[0].toUpperCase() + value.slice(1);
  return /[.!?]$/.test(first) ? first : `${first}.`;
}

function announce(text) {
  $('announcer').textContent = '';
  setTimeout(() => { $('announcer').textContent = text; }, 60);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { 'X-Smitline-Token': state.token, ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    });
  } catch {
    throw Object.assign(new Error('The console is not responding. Reload the page.'), { offline: true });
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok && !payload.fields) {
    throw Object.assign(new Error(payload.error || `Request failed (${response.status})`), { status: response.status });
  }
  return { ok: response.ok, ...payload };
}

function showBanner(title, detail) {
  $('banner').className = 'banner bad';
  $('banner').replaceChildren(element('strong', { textContent: title }), element('span', { textContent: detail }));
  $('banner').hidden = false;
}

function setConnection(live) {
  $('connection').classList.toggle('live', live);
  $('connection').classList.toggle('down', !live);
  $('connection-label').textContent = live ? 'Connected' : 'Console offline';
}

// Checklist --------------------------------------------------------------------

function checkMark(check) {
  if (check.ok === true) return { mark: '✓', tone: 'good', text: 'OK' };
  if (check.ok === null) return { mark: '?', tone: 'neutral', text: 'Could not check' };
  return check.required ? { mark: '!', tone: 'bad', text: 'Needs fixing' } : { mark: '!', tone: 'warn', text: 'Needs fixing' };
}

function renderChecks() {
  const status = state.status;
  if (!status) return;
  const list = $('checks');
  list.replaceChildren(...CHECK_GROUPS.flatMap(([group, title]) => {
    const checks = status.checks.filter((check) => (check.group || 'core') === group);
    if (!checks.length) return [];
    return [
      element('li', { className: 'check-group' }, element('h3', { textContent: title })),
      ...checks.map((check) => {
        const mark = checkMark(check);
        const detail = element('span', { className: 'check-detail', textContent: check.detail || '' });
        // Keys and settings are changed on this page; other fixes are commands, as setup status prints them.
        let fix = null;
        if (check.ok !== true && check.fix) {
          fix = /smitline setup (secrets|set)/.test(check.fix)
            ? element('span', { className: 'check-fix', textContent: 'Change it under Keys and settings below.' })
            : element('span', { className: 'check-fix' }, 'To fix: ', element('code', { textContent: check.fix }));
        }
        return element('li', { className: `check ${mark.tone}` },
          element('span', { className: `check-mark ${mark.tone}`, textContent: mark.mark, ariaHidden: 'true' }),
          element('span', { className: 'check-body' },
            element('span', { className: 'check-label' }, check.label, element('span', { className: 'visually-hidden', textContent: `: ${mark.text}.` })),
            detail, fix));
      }),
    ];
  }));
  const failing = status.checks.filter((check) => check.ok === false);
  const parts = [status.ready ? 'Ready for meetings' + (status.phoneReady ? ' and phone calls.' : '.') : 'Smitline is not ready yet.'];
  if (failing.length) parts.push(`${failing.length} ${failing.length === 1 ? 'item needs' : 'items need'} fixing.`);
  $('checklist-summary').textContent = parts.join(' ');
}

// Keys and settings -----------------------------------------------------------------

function savedText(field) {
  if (field.fromEnvironment) return 'Set in Smitline’s environment, so it can only be changed there';
  if (!field.saved) return 'Not set';
  if (!field.secret) return field.shown;
  return field.shown ? `Saved · ${field.shown}…` : 'Saved';
}

function noteNode(key) {
  const note = state.notes.get(key);
  if (!note) return null;
  const node = element('p', { className: `field-note ${note.tone}`, role: note.tone === 'bad' ? 'alert' : 'status' }, note.text);
  if (note.revoke) {
    node.append(' Revoke it at ', element('a', { href: note.revoke.url, target: '_blank', rel: 'noopener noreferrer', textContent: note.revoke.where }), '.');
  }
  return node;
}

function editForm(field) {
  const inputId = `value-${field.key}`;
  const phone = /PHONE|NUMBER|CALLER_ID/.test(field.key);
  const input = element('input', {
    id: inputId, name: field.key, type: field.secret ? 'password' : phone ? 'tel' : 'text',
    autocomplete: 'off', spellcheck: false, placeholder: field.secret ? 'Paste the new value' : (field.shown || ''),
  });
  input.setAttribute('autocapitalize', 'off');
  input.setAttribute('aria-describedby', `hint-${field.key}`);
  const form = element('form', { className: 'field-form' },
    element('label', { htmlFor: inputId, className: 'visually-hidden', textContent: `New ${field.label}` }),
    input,
    element('button', { type: 'submit', className: 'button primary', textContent: 'Save and check' }),
    element('button', { type: 'button', className: 'button', textContent: 'Cancel', onclick: () => close(field.key) }),
    element('p', { id: `hint-${field.key}`, className: 'muted small field-hint', textContent: field.hint }));
  form.addEventListener('submit', (event) => { event.preventDefault(); save(field, input); });
  return form;
}

function removeBox(field) {
  const box = element('div', { className: 'callout warn remove-box', role: 'group' });
  box.setAttribute('aria-label', `Remove ${field.label}`);
  const words = field.secret
    ? 'This deletes Smitline’s saved copy only. The key keeps working where it was issued until you revoke it there.'
    : 'This deletes the setting from Smitline.';
  box.append(element('p', { className: 'callout-title', textContent: `Remove ${field.label} from Smitline?` }), element('p', { textContent: words }));
  if (field.secret && field.revoke) {
    box.append(element('p', {}, 'To revoke it, go to ', element('a', { href: field.revoke.url, target: '_blank', rel: 'noopener noreferrer', textContent: field.revoke.where }), '.'));
  }
  box.append(element('div', { className: 'actions' },
    element('button', { type: 'button', className: 'button danger', id: `confirm-${field.key}`, textContent: 'Remove from Smitline', onclick: () => remove(field) }),
    element('button', { type: 'button', className: 'button', textContent: 'Cancel', onclick: () => close(field.key) })));
  return box;
}

function fieldRow(field) {
  const open = state.open === field.key;
  const status = element('span', { className: `field-state${field.saved ? ' saved' : ''}`, textContent: savedText(field) });
  const buttons = element('span', { className: 'field-buttons' });
  if (!field.fromEnvironment && !open) {
    buttons.append(element('button', {
      type: 'button', className: 'button', id: `edit-${field.key}`,
      textContent: field.saved ? (field.secret ? 'Replace' : 'Change') : 'Add',
      onclick: () => openField(field.key, 'edit'),
    }));
    if (field.saved) {
      buttons.append(element('button', {
        type: 'button', className: 'button danger', id: `remove-${field.key}`, textContent: 'Remove',
        onclick: () => openField(field.key, 'remove'),
      }));
    }
    buttons.querySelectorAll('button').forEach((button) => button.setAttribute('aria-label', `${button.textContent} ${field.label}`));
  }
  return element('li', { className: 'field-row' },
    element('div', { className: 'field-line' },
      element('span', { className: 'field-name' }, element('span', { className: 'field-label', textContent: field.label }), status),
      buttons),
    open && state.mode === 'edit' ? editForm(field) : null,
    open && state.mode === 'remove' ? removeBox(field) : null,
    noteNode(field.key));
}

function renderFields() {
  const groups = [...new Set(state.fields.map((field) => field.group))];
  $('field-groups').replaceChildren(...groups.map((group) => {
    const fields = state.fields.filter((field) => field.group === group);
    const list = element('ul', { className: 'field-list' }, ...fields.map(fieldRow));
    const title = GROUP_TITLES[group] || group;
    // Rarely used groups stay folded unless something in them is saved or open.
    if (/\((server mode|upgraded account|advanced)\)$/.test(group)) {
      const details = element('details', { className: 'card field-group' }, element('summary', { textContent: title }), list);
      details.open = fields.some((field) => field.saved || field.key === state.open || state.notes.has(field.key));
      return details;
    }
    return element('section', { className: 'card field-group' }, element('h3', { textContent: title }), list);
  }));
}

function render() {
  renderChecks();
  renderFields();
}

function openField(key, mode) {
  state.open = key;
  state.mode = mode;
  state.notes.delete(key);
  render();
  $(mode === 'edit' ? `value-${key}` : `confirm-${key}`)?.focus();
}

function close(key) {
  state.open = null;
  state.mode = null;
  render();
  $(`edit-${key}`)?.focus();
}

function apply(view) {
  if (view.fields) state.fields = view.fields;
  if (view.status) state.status = view.status;
}

function checkNote(key) {
  const check = state.status?.checks.find((item) => item.id === CHECK_FOR[key]);
  if (!check) return { tone: 'good', text: 'Saved.' };
  const tone = check.ok === true ? 'good' : check.ok === null ? 'neutral' : 'bad';
  return { tone, text: `Saved. ${check.label}: ${sentence(check.detail)}` };
}

async function save(field, input) {
  if (state.busy) return;
  const value = input.value;
  if (!value.trim()) {
    state.notes.set(field.key, { tone: 'bad', text: `Enter the new ${field.label}, or press Cancel.` });
    render();
    $(`value-${field.key}`)?.focus();
    return;
  }
  state.busy = true;
  input.disabled = true;
  state.notes.set(field.key, { tone: 'neutral', text: 'Saving and checking…' });
  try {
    const result = await api('/api/setup/save', { method: 'POST', body: JSON.stringify({ values: { [field.key]: value } }) });
    apply(result);
    const problem = (result.errors || []).find((error) => !error.key || error.key === field.key);
    if (problem) {
      state.notes.set(field.key, { tone: 'bad', text: `${sentence(problem.message)} Enter it again.` });
      render();
      $(`value-${field.key}`)?.focus();
    } else {
      state.open = null;
      state.mode = null;
      state.notes.set(field.key, checkNote(field.key));
      render();
      $(`edit-${field.key}`)?.focus();
      announce(state.notes.get(field.key).text);
    }
  } catch (error) {
    state.notes.set(field.key, { tone: 'bad', text: error.status === 403 ? 'This page is out of date. Reload it and try again.' : sentence(error.message) });
    render();
  } finally {
    state.busy = false;
  }
}

async function remove(field) {
  if (state.busy) return;
  state.busy = true;
  try {
    const result = await api('/api/setup/remove', { method: 'POST', body: JSON.stringify({ key: field.key }) });
    if (!result.ok) throw new Error(result.error || 'It could not be removed.');
    apply(result);
    state.open = null;
    state.mode = null;
    state.notes.set(field.key, field.secret
      ? { tone: 'neutral', text: 'Removed from Smitline. It still works where it was issued.', revoke: field.revoke }
      : { tone: 'neutral', text: 'Removed from Smitline.' });
    render();
    $(`edit-${field.key}`)?.focus();
    announce(`${field.label} removed from Smitline.`);
  } catch (error) {
    state.notes.set(field.key, { tone: 'bad', text: error.status === 403 ? 'This page is out of date. Reload it and try again.' : sentence(error.message) });
    render();
  } finally {
    state.busy = false;
  }
}

async function check({ verify = true } = {}) {
  $('recheck-button').disabled = true;
  $('checklist-summary').textContent = verify ? 'Checking your keys with OpenAI and your phone provider…' : 'Loading…';
  try {
    apply(await api(`/api/setup${verify ? '' : '?verify=0'}`));
    setConnection(true);
    $('banner').hidden = true;
    render();
  } catch (error) {
    if (error.offline) setConnection(false);
    showBanner('Couldn’t load your setup.', sentence(error.message));
    $('checklist-summary').textContent = '';
  } finally {
    $('recheck-button').disabled = false;
  }
}

$('recheck-button').addEventListener('click', () => check().then(() => announce($('checklist-summary').textContent)));

async function init() {
  try {
    const bootstrap = await fetch('/api/bootstrap').then((response) => response.json());
    state.token = bootstrap.token || '';
  } catch {
    setConnection(false);
    showBanner('The console is not responding.', 'Start it again, then reload this page.');
    return;
  }
  // Show what is saved at once, then verify the keys online.
  await check({ verify: false });
  await check();
}

init();
