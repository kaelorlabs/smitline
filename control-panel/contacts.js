// Contacts: everyone called or calling, what the user saved about them, and what the next call
// to them would start with. Reading needs no token, like Calls; saving takes the console token.
const $ = (id) => document.getElementById(id);
const NARROW = window.matchMedia('(max-width: 760px)');
const state = { token: '', contacts: [], selected: decodeURIComponent(location.hash.slice(1)) || null, contact: null, query: '' };

function element(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { 'X-Smitline-Token': state.token, ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
    });
  } catch {
    throw Object.assign(new Error('The console is not responding.'), { code: 'console_offline' });
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(String(payload.error || `Request failed (${response.status})`)), { status: response.status, code: payload.code });
  return payload;
}

const dayFormat = new Intl.DateTimeFormat([], { month: 'short', day: 'numeric', year: 'numeric' });

function day(iso) {
  const date = new Date(iso || '');
  return Number.isNaN(date.getTime()) ? '' : dayFormat.format(date);
}

function formatPhone(value) {
  const match = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(value || '');
  return match ? `+1 ${match[1]} ${match[2]} ${match[3]}` : (value || '');
}

function callCount(count) {
  return count === 1 ? '1 call' : `${count} calls`;
}

function announce(text) {
  $('announcer').textContent = '';
  setTimeout(() => { $('announcer').textContent = text; }, 60);
}

function showBanner(title, detail) {
  $('banner').className = 'banner bad';
  $('banner').replaceChildren(element('strong', { textContent: title }), element('span', { textContent: detail }));
  $('banner').hidden = false;
}

function setConnection(live) {
  $('connection').classList.toggle('live', live);
  $('connection').classList.toggle('down', !live);
  $('connection-label').textContent = live ? 'Connected' : 'Smitline not running';
}

function setMode() {
  const mode = state.selected ? 'detail' : 'list';
  $('layout').dataset.mode = mode;
  document.body.dataset.mode = mode;
}

// List ----------------------------------------------------------------------------

function matches(contact) {
  const query = state.query.trim().toLowerCase();
  if (!query) return true;
  const digits = query.replace(/[^\d+]/g, '');
  return contact.name.toLowerCase().includes(query) || (digits && contact.number.includes(digits));
}

function renderList() {
  const shown = state.contacts.filter(matches);
  $('contact-list').replaceChildren(...shown.map((contact) => {
    const button = element('button', { type: 'button', className: 'call-item', onclick: () => open(contact.number) },
      element('span', { className: 'top' },
        element('span', { className: 'who', textContent: contact.name || formatPhone(contact.number) }),
        contact.autoContext ? element('span', { className: 'chip progress', textContent: 'Auto context' }) : null),
      element('span', { className: 'goal', textContent: contact.name ? formatPhone(contact.number) : (contact.lastObjective || '') }),
      element('span', { className: 'row' },
        element('span', { className: 'muted small', textContent: callCount(contact.calls) }),
        element('time', { dateTime: contact.lastCallAt || '', textContent: contact.lastCallAt ? `Last ${day(contact.lastCallAt)}` : '' })));
    button.setAttribute('aria-current', String(contact.number === state.selected));
    return element('li', {}, button);
  }));
  $('list-loading').hidden = true;
  $('list-empty').hidden = state.contacts.length > 0;
  $('list-filtered').hidden = !state.contacts.length || shown.length > 0;
}

// Detail --------------------------------------------------------------------------

function renderContact(contact) {
  $('pick').hidden = true;
  $('contact').hidden = false;
  $('contact-title').textContent = contact.name || formatPhone(contact.number);
  document.title = `${contact.name || formatPhone(contact.number)} · Contacts · Smitline`;
  $('contact-meta').textContent = [formatPhone(contact.number), callCount(contact.calls),
    contact.lastCallAt ? `last ${day(contact.lastCallAt)}` : ''].filter(Boolean).join(' · ');
  $('contact-name').value = contact.name || '';
  $('contact-notes').value = contact.notes || '';
  $('contact-auto').checked = Boolean(contact.autoContext);
  $('form-note').textContent = '';
  $('next-call').replaceChildren(...(contact.nextCall || []).map((note) => element('li', {},
    element('span', { className: 'carried-from', textContent: `${note.contact}${note.at ? `, ${day(note.at)}` : ''}` }),
    element('span', { textContent: note.text }),
    element('span', { className: 'muted small', textContent: note.by === 'agent' ? 'Note from your agent' : 'From the call’s summary' }))));
  $('next-empty').hidden = Boolean(contact.nextCall?.length);
  $('history').replaceChildren(...(contact.history || []).map((call) => element('li', {},
    element('a', { href: `/calls#${call.id}`, textContent: call.objective || 'Call' }),
    element('span', { className: 'muted small', textContent: [day(call.createdAt), call.direction === 'inbound' ? 'they called' : '',
      call.task?.title || call.task?.id || '', call.outcome ? call.outcome.replace(/_/g, ' ') : call.status].filter(Boolean).join(' · ') }),
    call.summary ? element('span', { textContent: call.summary }) : null)));
  $('forget-button').parentElement.hidden = !contact.saved;
}

async function loadContact(number) {
  try {
    const contact = await api(`/api/contacts/${encodeURIComponent(number)}`);
    if (state.selected !== number) return;
    state.contact = contact;
    renderContact(contact);
  } catch (error) {
    if (error.status === 404) {
      showList();
      $('pick').textContent = 'That contact is no longer available. Choose another one.';
      return;
    }
    showBanner('Couldn’t load the contact.', error.message);
  }
}

function open(number) {
  if (NARROW.matches) history.pushState({ fromList: true }, '', `#${encodeURIComponent(number)}`);
  else history.replaceState(null, '', `#${encodeURIComponent(number)}`);
  state.selected = number;
  setMode();
  renderList();
  loadContact(number).then(() => { if (NARROW.matches) $('contact-title').focus(); });
}

function showList() {
  state.selected = null;
  state.contact = null;
  history.replaceState(null, '', location.pathname);
  $('contact').hidden = true;
  $('pick').hidden = false;
  setMode();
  renderList();
}

async function refresh() {
  try {
    const { contacts = [] } = await api('/api/contacts');
    state.contacts = contacts;
    setConnection(true);
    $('banner').hidden = true;
    if (!state.selected && contacts.length && !NARROW.matches) state.selected = contacts[0].number;
    renderList();
    setMode();
    if (state.selected) await loadContact(state.selected);
    else $('pick').hidden = !contacts.length;
  } catch (error) {
    $('list-loading').hidden = true;
    setConnection(false);
    showBanner(error.code === 'console_offline' ? 'The console stopped responding.' : 'Smitline isn’t running right now, so contacts can’t be shown.',
      error.code === 'console_offline' ? 'Start it again, then reload this page.' : 'It starts by itself when your agent places a call.');
  }
}

$('contact-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const number = state.selected;
  if (!number) return;
  $('save-button').disabled = true;
  try {
    const contact = await api(`/api/contacts/${encodeURIComponent(number)}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: $('contact-name').value.trim(), notes: $('contact-notes').value.trim(), autoContext: $('contact-auto').checked }),
    });
    state.contact = contact;
    renderContact(contact);
    $('form-note').textContent = 'Saved.';
    announce('Contact saved.');
    const { contacts = [] } = await api('/api/contacts');
    state.contacts = contacts;
    renderList();
  } catch (error) {
    $('form-note').textContent = error.status === 403 ? 'This page is out of date. Reload it and try again.' : `That didn’t work: ${error.message}`;
  } finally {
    $('save-button').disabled = false;
  }
});

$('forget-button').addEventListener('click', async () => {
  const number = state.selected;
  if (!number || !window.confirm('Forget the name, notes, and setting you saved for this contact? Its calls stay.')) return;
  try {
    await api(`/api/contacts/${encodeURIComponent(number)}`, { method: 'DELETE' });
    announce('Saved details forgotten.');
    await refresh();
  } catch (error) {
    $('form-note').textContent = `That didn’t work: ${error.message}`;
  }
});

$('search').addEventListener('input', () => {
  state.query = $('search').value;
  renderList();
});

$('back-button').addEventListener('click', () => {
  if (history.state?.fromList) history.back();
  else showList();
});

window.addEventListener('popstate', () => {
  const number = decodeURIComponent(location.hash.slice(1)) || null;
  if (!number) showList();
  else if (number !== state.selected) open(number);
});
NARROW.addEventListener('change', setMode);

fetch('/api/bootstrap').then((response) => response.json()).then((bootstrap) => { state.token = bootstrap.token || ''; }).catch(() => {});
refresh();
