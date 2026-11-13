import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {
  CALL_CARD_MIME, CALL_CARD_URI, callCardHtml, callCardResource, callCardView,
} from '../src/call-card.mjs';
import { createMcpSession, TOOL_DEFINITIONS } from '../src/session.mjs';

const ID = 'call-0123456789abcdef';
const DONE = {
  id: ID,
  channel: 'phone',
  status: 'completed',
  brief: { to: '+15550100', onBehalfOf: 'Ankit', contact: { name: 'Bawarchi' } },
  answeredAt: '2026-11-02T17:24:38Z',
  endedAt: '2026-11-02T17:27:13Z',
  result: {
    outcome: 'achieved',
    summary: 'They took the order for pickup at 6:15.',
    details: [{ label: 'Total', value: '$24.13' }, { label: 'Empty', value: '' }],
    actionItems: ['Pick up at 6:15'],
    durationSeconds: 155,
  },
  transcript: [{ speaker: 'agent', text: 'Hi, calling for Ankit.' }, { speaker: 'other', text: 'Sure.' }],
  charge: { amount: '$0.66', billedSeconds: 198, pricePerMinute: '$0.20', status: 'charged' },
  recording: { seconds: 155, format: 'wav', source: 'smitline' },
  recordingUrl: 'https://app.example.com/r/abc',
  callUrl: 'https://app.example.com/app/calls/call-0123456789abcdef',
};

test('the view says how a hosted call went and where the recording is, not what it cost', () => {
  const view = callCardView(DONE);
  assert.equal(view.title, 'Call to Bawarchi');
  assert.equal(view.subtitle, '+15550100 · 2 min 35 s');
  assert.deepEqual([view.status, view.tone, view.live], ['Done', 'done', false]);
  assert.equal(view.outcome, 'Got it done');
  assert.deepEqual(view.details, [{ label: 'Total', value: '$24.13' }]);
  assert.deepEqual(view.lists, [{ title: 'Action items', items: ['Pick up at 6:15'] }]);
  assert.equal('cost' in view, false);
  assert.deepEqual(view.recording, { url: 'https://app.example.com/r/abc', length: '2:35' });
  assert.deepEqual(view.transcript.map((line) => line.who), ['Smitline', 'Bawarchi']);
  assert.equal(view.callUrl, DONE.callUrl);
});

test('a note in the transcript, such as a hold, is shown as a note, not as said by them', () => {
  const view = callCardView({ ...DONE, transcript: [...DONE.transcript, { speaker: 'note', text: 'On hold.' }] });
  assert.deepEqual(view.transcript.map((line) => [line.who, line.text]),
    [['Smitline', 'Hi, calling for Ankit.'], ['Bawarchi', 'Sure.'], ['Note', 'On hold.']]);
});

test('the view of a self-hosted or live call: no provider cost, no player without a safe link', () => {
  const live = callCardView({
    id: ID, status: 'in_progress', brief: { to: '+15550100' }, answeredAt: '2026-11-02T17:00:00Z',
    cost: { total: 0.2198, estimated: true },
    recording: { seconds: 7 }, recordingUrl: 'http://insecure.example.com/r.wav', callUrl: 'javascript:alert(1)',
  }, Date.parse('2026-11-02T17:00:42Z'));
  assert.deepEqual([live.title, live.status, live.live], ['Call to +15550100', 'On the call', true]);
  assert.equal(live.subtitle, '42 s');
  assert.equal('cost' in live, false);
  assert.deepEqual(live.recording, { url: null, length: '0:07' });
  assert.equal(live.callUrl, null);
  const failed = callCardView({ id: ID, status: 'failed', error: 'The number is not in service.' });
  assert.deepEqual([failed.status, failed.tone, failed.summary], ['Failed', 'bad', 'The number is not in service.']);
  assert.equal(callCardView({ id: ID, channel: 'meeting', status: 'waiting' }).title, 'Meeting');
  assert.equal(callCardView(null).title, 'Call to someone');
});

test('start_call and get_call show the card, served as a self-contained MCP Apps resource', async () => {
  const session = createMcpSession({
    colleague: {}, log() {}, card: { mediaOrigins: ['https://app.example.com', 'http://plain.example.com', 'javascript:x'] },
  });
  const init = await session.dispatch({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } });
  assert.deepEqual(init.result.capabilities.resources, { listChanged: false });
  const withCard = TOOL_DEFINITIONS.filter((tool) => tool._meta?.ui?.resourceUri).map((tool) => tool.name);
  assert.deepEqual(withCard, ['start_call', 'get_call']);
  for (const name of withCard) {
    const { _meta } = TOOL_DEFINITIONS.find((tool) => tool.name === name);
    assert.equal(_meta.ui.resourceUri, CALL_CARD_URI);
    assert.equal(_meta['openai/outputTemplate'], CALL_CARD_URI);
  }
  // The card answers from itself with these two, so ChatGPT must let it call them.
  const fromCard = TOOL_DEFINITIONS.filter((tool) => tool._meta?.['openai/widgetAccessible']).map((tool) => tool.name);
  assert.deepEqual(fromCard.sort(), ['get_call', 'reply_to_call_request', 'send_call_instruction', 'start_call']);
  const listed = await session.dispatch({ jsonrpc: '2.0', id: 2, method: 'resources/list' });
  assert.deepEqual(listed.result.resources.map((item) => [item.uri, item.mimeType]), [[CALL_CARD_URI, CALL_CARD_MIME]]);
  const read = await session.dispatch({ jsonrpc: '2.0', id: 3, method: 'resources/read', params: { uri: CALL_CARD_URI } });
  const [content] = read.result.contents;
  assert.equal(content.mimeType, 'text/html;profile=mcp-app');
  assert.deepEqual(content._meta.ui.csp, { connectDomains: [], resourceDomains: ['https://app.example.com'] });
  assert.match(content.text, /^<!doctype html>/);
  assert.doesNotMatch(content.text, /<script[^>]*\ssrc=|<link\b|@import|https?:\/\/(?!app\.example)/i);
  const missing = await session.dispatch({ jsonrpc: '2.0', id: 4, method: 'resources/read', params: { uri: 'ui://other' } });
  assert.equal(missing.error.code, -32002);
  const templates = await session.dispatch({ jsonrpc: '2.0', id: 5, method: 'resources/templates/list' });
  assert.deepEqual(templates.result, { resourceTemplates: [] });
  assert.deepEqual(callCardResource().content._meta.ui.csp.resourceDomains, []);
});

// A small stand-in for the browser: just what the card uses.
class FakeText {
  constructor(text) { this.textContent = String(text); }
}

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = '';
    this.dataset = {};
    this.listeners = {};
    this.own = '';
    this.style = { set: {}, setProperty(name, value) { this.set[name] = value; } };
    this.attributes = {};
  }

  set textContent(value) { this.children = []; this.own = String(value); }

  get textContent() { return this.own + this.children.map((child) => child.textContent).join(''); }

  append(...nodes) { for (const item of nodes) this.children.push(typeof item === 'string' ? new FakeText(item) : item); }

  replaceChildren(...nodes) { this.children = []; this.own = ''; this.append(...nodes); }

  get childElementCount() { return this.children.filter((child) => child instanceof FakeElement).length; }

  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }

  all() { return [this, ...this.children.flatMap((child) => (child instanceof FakeElement ? child.all() : []))]; }

  setAttribute(name, value) { this.attributes[name] = String(value); }

  querySelectorAll(tag) { return this.all().filter((element) => element.tagName === tag.toUpperCase()); }
}

function loadCard() {
  const html = callCardHtml();
  const script = html.match(/<script type="module">([\s\S]*)<\/script>/)[1];
  const main = new FakeElement('main');
  const root = new FakeElement('html');
  const sent = [];
  const timers = [];
  const listeners = {};
  const parent = { postMessage(message) { sent.push(JSON.parse(JSON.stringify(message))); } };
  const window = {
    parent,
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
  };
  const head = new FakeElement('head');
  const context = vm.createContext({
    window,
    document: {
      documentElement: root,
      head,
      getElementById: (id) => head.all().find((element) => element.id === id) || null,
      querySelector: (selector) => (selector === 'main' ? main : null),
      createElement: (tag) => new FakeElement(tag),
      createTextNode: (text) => new FakeText(text),
    },
    ResizeObserver: class { observe() {} },
    setTimeout: (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length; },
    clearTimeout: (handle) => { if (timers[handle - 1]) timers[handle - 1].live = false; },
    URL,
    Date,
    Promise,
  });
  vm.runInContext(script, context);
  const deliverFrom = (source, data) => { for (const listener of listeners.message || []) listener({ source, data }); };
  const deliver = (data) => deliverFrom(parent, data);
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  return { main, root, head, sent, timers, deliver, deliverFrom, settle };
}

test('the card follows a live call through the MCP Apps bridge, and shows text as text', async () => {
  const card = loadCard();
  const [hello] = card.sent;
  assert.equal(hello.method, 'ui/initialize');
  // MCP Apps hosts reject any other fields (no clientInfo or capabilities, as in MCP's initialize).
  assert.deepEqual(Object.keys(hello.params).sort(), ['appCapabilities', 'appInfo', 'protocolVersion']);
  assert.deepEqual(hello.params.appInfo, { name: 'smitline-call-card', version: '1' });
  assert.equal(hello.params.protocolVersion, '2026-01-26');
  card.deliver({ jsonrpc: '2.0', id: hello.id, result: { hostContext: { theme: 'dark' } } });
  await card.settle();
  assert.equal(card.root.dataset.theme, 'dark');
  assert.ok(card.sent.some((message) => message.method === 'ui/notifications/initialized'));

  const name = '<img src=x onerror=alert(1)>';
  card.deliver({
    jsonrpc: '2.0', method: 'ui/notifications/tool-result',
    params: { content: [], structuredContent: { id: ID, status: 'ringing', brief: { to: '+15550100', contact: { name } } } },
  });
  assert.match(card.main.textContent, /Ringing/);
  assert.ok(card.main.textContent.includes(`Call to ${name}`));
  assert.ok(card.main.all().every((element) => element.tagName !== 'IMG'));

  // While it runs, the card asks for the call again by itself.
  // Answered requests leave no timeouts behind: only the refresh is pending.
  assert.equal(card.timers.filter((timer) => timer.live && timer.ms !== 3000).length, 0);
  const pending = card.timers.filter((timer) => timer.live);
  assert.equal(pending.length, 1);
  pending[0].live = false;
  pending[0].fn();
  const refresh = card.sent.at(-1);
  assert.deepEqual([refresh.method, refresh.params], ['tools/call', { name: 'get_call', arguments: { callId: ID } }]);
  card.deliver({ jsonrpc: '2.0', id: refresh.id, result: { content: [], structuredContent: DONE } });
  await card.settle();
  const shown = card.main.textContent;
  for (const part of ['Done', 'Got it done', 'Transcript · 2', 'Open in Smitline']) {
    assert.ok(shown.includes(part), part);
  }
  for (const part of ['Cost', '$0.66', '$0.20/min']) assert.ok(!shown.includes(part), part);
  const [audio] = card.main.all().filter((element) => element.tagName === 'AUDIO');
  assert.equal(audio.src, 'https://app.example.com/r/abc');
  assert.equal(card.timers.filter((timer) => timer.live).length, 0); // done: no more refreshes

  const [open] = card.main.all().filter((element) => element.tagName === 'BUTTON');
  open.listeners.click[0]();
  assert.deepEqual(card.sent.at(-1).params, { url: DONE.callUrl });
  assert.equal(card.sent.at(-1).method, 'ui/open-link');

  card.deliver({ jsonrpc: '2.0', id: 99, method: 'ui/resource-teardown', params: { reason: 'closed' } });
  assert.deepEqual(card.sent.at(-1), { jsonrpc: '2.0', id: 99, result: {} });
});

test('messages from anywhere but the host are ignored', () => {
  const card = loadCard();
  const result = { jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: DONE } };
  card.deliverFrom({ postMessage() {} }, result); // another frame or window
  assert.equal(card.main.childElementCount, 0);
  card.deliver({ jsonrpc: '1.0', method: 'ui/notifications/tool-result', params: { structuredContent: DONE } });
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: { id: 'not-a-call' } } });
  assert.equal(card.main.childElementCount, 0);
  card.deliver(result);
  assert.ok(card.main.textContent.includes('Call to Bawarchi'));
});

test('the view lists the questions from a running call that nobody has finished', () => {
  const running = {
    id: ID, status: 'in_progress', brief: { to: '+15550100' },
    requests: [
      { id: 'r1', state: 'open', text: 'Should I bring anything?', askedBy: 'Sam',
        thread: [{ from: 'call', type: 'ask', text: 'Should I bring anything?' },
          { from: 'agent', type: 'status', text: 'Checking' }, { from: 'call', type: 'add', text: 'Drinks, maybe?' }] },
      { id: 'r2', state: 'working', text: 'What time?' },
      { id: 'r3', state: 'done', text: 'Answered already' },
      { id: 'r4', state: 'canceled', text: 'Dropped' },
      { id: 'x5', state: 'open', text: 'Not a request id' },
      { id: 'r6', state: 'open', text: '  ' },
    ],
  };
  const view = callCardView(running);
  assert.deepEqual(view.requests, [
    { id: 'r1', text: 'Should I bring anything?', added: 'Drinks, maybe?', askedBy: 'Sam' },
    { id: 'r2', text: 'What time?', added: '', askedBy: '' },
  ]);
  assert.equal(view.canTell, true);
  assert.deepEqual(callCardView({ ...running, status: 'completed' }).requests, []); // openRequests, for the agent
  assert.equal(callCardView({ ...running, status: 'ringing' }).canTell, false);
});

test('the user answers a question and tells the call something from the card', async () => {
  const card = loadCard();
  const [hello] = card.sent;
  card.deliver({ jsonrpc: '2.0', id: hello.id, result: {} });
  await card.settle();
  const live = {
    id: ID, status: 'in_progress', brief: { to: '+15550100' },
    requests: [{ id: 'r1', state: 'open', text: 'Should I bring anything?', askedBy: 'Sam' }],
  };
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: live } });
  assert.ok(card.main.textContent.includes('Sam asks'));
  assert.ok(card.main.textContent.includes('Should I bring anything?'));
  const box = (key) => card.main.querySelectorAll('textarea').find((item) => item.dataset.key === key);
  const submit = (key) => {
    const form = card.main.querySelectorAll('form').find((item) => item.children[0].dataset.key === key);
    form.listeners.submit[0]({ preventDefault() {} });
  };
  const type = (key, words) => { box(key).value = words; box(key).listeners.input[0](); };
  assert.ok(box('r1'));
  assert.equal(box('tell'), undefined); // one box at a time: the question first

  type('r1', 'Just yourself');
  submit('r1');
  const reply = card.sent.at(-1);
  assert.deepEqual([reply.method, reply.params], ['tools/call', {
    name: 'reply_to_call_request', arguments: { callId: ID, requestId: 'r1', type: 'done', text: 'Just yourself' },
  }]);
  assert.ok(card.main.textContent.includes('Sending…'));
  submit('r1'); // a second press while it sends does nothing
  assert.equal(card.sent.at(-1), reply);
  card.deliver({ jsonrpc: '2.0', id: reply.id, result: { content: [], isError: false } });
  await card.settle();
  assert.ok(card.main.textContent.includes('Sent to the call.'));
  assert.equal(box('r1').value, '');
  const again = card.sent.at(-1); // and it asks for the call again at once
  assert.deepEqual([again.method, again.params.name], ['tools/call', 'get_call']);
  const closed = { ...live, requests: [{ ...live.requests[0], state: 'done' }] };
  card.deliver({ jsonrpc: '2.0', id: again.id, result: { content: [], structuredContent: closed } });
  await card.settle();
  assert.equal(box('r1'), undefined); // the question has gone; what the user said stays
  assert.ok(card.main.textContent.includes('Replied “Just yourself” to “Should I bring anything?”'));
  assert.ok(box('tell')); // and with no question open, "Tell the call" takes its place

  const before = card.sent.length;
  submit('tell'); // an empty box sends nothing
  assert.equal(card.sent.length, before);
  type('tell', 'Say we are 10 minutes late');
  submit('tell');
  const note = card.sent.at(-1);
  assert.deepEqual(note.params, { name: 'send_call_instruction', arguments: { callId: ID, text: 'Say we are 10 minutes late' } });
  const refused = JSON.stringify({ code: 'call_not_active', message: 'The call has ended.' });
  card.deliver({ jsonrpc: '2.0', id: note.id, result: { isError: true, content: [{ type: 'text', text: refused }] } });
  await card.settle();
  assert.ok(card.main.textContent.includes('Not sent: The call has ended.'));
  assert.equal(box('tell').value, 'Say we are 10 minutes late'); // kept, to try again
});

test('the card asks one question at a time, oldest first, and says how many wait', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const live = {
    id: ID, status: 'in_progress', brief: { to: '+15550100' },
    requests: [
      { id: 'r1', state: 'open', text: 'Should I bring anything?', askedBy: 'Sam' },
      { id: 'r2', state: 'open', text: 'Is 5 still fine?', askedBy: 'Sam' },
      { id: 'r3', state: 'working', text: 'Can you send the address?', askedBy: 'Sam' },
    ],
  };
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: live } });
  const keys = card.main.querySelectorAll('textarea').map((item) => item.dataset.key);
  assert.deepEqual(keys, ['r1']);
  assert.ok(card.main.textContent.includes('2 more waiting'));
  assert.equal(card.main.querySelectorAll('button').length, 1); // Answer; no Send while a question waits
});

test('the card takes the host’s colors, fonts and theme, and keeps its own for what the host leaves out', async () => {
  const card = loadCard();
  card.deliver({
    jsonrpc: '2.0', id: card.sent[0].id, result: {
      hostContext: {
        theme: 'dark',
        styles: {
          variables: {
            '--color-text-primary': 'light-dark(#111, #eee)', '--font-sans': '"Host Sans", sans-serif',
            'color': 'red', '--Bad Name': 'x', '--color-text-secondary': 42, '--color-text-info': 'x'.repeat(501),
          },
          css: { fonts: '@font-face { font-family: "Host Sans"; src: local("Host Sans"); }' },
        },
      },
    },
  });
  await card.settle();
  assert.equal(card.root.dataset.theme, 'dark');
  assert.deepEqual(card.root.style.set, {
    '--color-text-primary': 'light-dark(#111, #eee)', '--font-sans': '"Host Sans", sans-serif',
  });
  const [fonts] = card.head.children;
  assert.equal(fonts.id, 'host-fonts');
  assert.match(fonts.textContent, /Host Sans/);
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: {
    theme: 'light', styles: { variables: { '--color-text-primary': '#111' }, css: { fonts: '@font-face {}' } },
  } });
  assert.equal(card.root.dataset.theme, 'light');
  assert.equal(card.root.style.set['--color-text-primary'], '#111');
  assert.equal(card.head.children.length, 1); // fonts once

  // Every color, font and corner in the stylesheet goes through a host variable with a fallback.
  const sheet = callCardHtml().match(/<style>([\s\S]*?)<\/style>/)[1];
  for (const name of ['--color-text-primary', '--color-text-secondary', '--color-border-primary', '--color-background-secondary',
    '--color-background-inverse', '--color-text-inverse', '--font-sans', '--border-radius-md', '--font-text-md-size']) {
    assert.match(sheet, new RegExp(`var\\(${name}, [^)]`), name);
  }
  const rules = sheet.replace(/:root[^{]*\{[^}]*\}/g, '').replace(/@media[^{]*\{\s*:root[^{]*\{[^}]*\}\s*\}/g, '');
  assert.doesNotMatch(rules, /#[0-9a-f]{3,6}\b|rgba?\(|font-size: \d|border-radius: \d+px/i);
});

test('an opened transcript stays open while the card follows the call', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const live = { id: ID, status: 'in_progress', brief: { to: '+15550100' }, transcript: [{ speaker: 'other', text: 'Hello?' }] };
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: live } });
  const details = () => card.main.querySelectorAll('details')[0];
  assert.equal(details().open, false);
  details().open = true;
  details().listeners.toggle[0]();
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: live } });
  assert.equal(details().open, true);
});
