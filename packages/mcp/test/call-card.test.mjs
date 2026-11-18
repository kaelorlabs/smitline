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
  assert.deepEqual(fromCard.sort(), ['get_call', 'get_call_update', 'reply_to_call_request', 'send_call_instruction', 'start_call']);
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

  removeAttribute(name) { delete this.attributes[name]; }

  querySelectorAll(tag) { return this.all().filter((element) => element.tagName === tag.toUpperCase()); }
}

// A stand-in host: the page's window, document and timers, with a clock the test moves.
// openai: what ChatGPT puts on window.openai (toolOutput, widgetState, setWidgetState, callTool).
function loadCard({ openai } = {}) {
  const html = callCardHtml();
  const script = html.match(/<script type="module">([\s\S]*)<\/script>/)[1];
  const main = new FakeElement('main');
  const root = new FakeElement('html');
  const sent = [];
  const timers = [];
  const listeners = {};
  const documentListeners = {};
  const observers = [];
  const clock = { now: Date.parse('2026-11-18T15:00:00Z') };
  class FakeDate extends Date {
    constructor(...args) { if (args.length) super(...args); else super(clock.now); }
    static now() { return clock.now; }
  }
  const parent = { postMessage(message) { sent.push(JSON.parse(JSON.stringify(message))); } };
  const window = {
    parent,
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
  };
  if (openai) window.openai = openai;
  const head = new FakeElement('head');
  const document = {
    hidden: false,
    documentElement: root,
    head,
    getElementById: (id) => head.all().find((element) => element.id === id) || null,
    querySelector: (selector) => (selector === 'main' ? main : null),
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => new FakeText(text),
    addEventListener(type, listener) { (documentListeners[type] ||= []).push(listener); },
  };
  const context = vm.createContext({
    window,
    document,
    ResizeObserver: class { observe() {} },
    IntersectionObserver: class {
      constructor(callback) { observers.push(callback); }
      observe() {}
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms, at: clock.now + ms, live: true }); return timers.length; },
    clearTimeout: (handle) => { if (timers[handle - 1]) timers[handle - 1].live = false; },
    URL,
    Date: FakeDate,
    Math: Object.assign(Object.create(Math), { random: () => 0.5 }), // no jitter: 0.9 + 0.2 * 0.5 = 1
    Promise,
  });
  vm.runInContext(script, context);
  const deliverFrom = (source, data) => { for (const listener of listeners.message || []) listener({ source, data }); };
  const deliver = (data) => deliverFrom(parent, data);
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  const fire = (type) => { for (const listener of listeners[type] || []) listener({}); };
  const setHidden = (hidden) => {
    document.hidden = hidden;
    for (const listener of documentListeners.visibilitychange || []) listener({});
  };
  const setOnScreen = (seen) => { for (const callback of observers) callback([{ isIntersecting: seen }]); };
  // Move the clock on, running each timer that comes due, in order.
  const advance = async (ms) => {
    const until = clock.now + ms;
    for (;;) {
      const next = timers.filter((timer) => timer.live && timer.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!next) break;
      clock.now = next.at;
      next.live = false;
      next.fn();
      await settle();
    }
    clock.now = until;
  };
  // The card's own timers, without the bridge's answer timeouts or the wait for a first call.
  const pending = () => timers.filter((timer) => timer.live && !/waiting\.delete|gone\(\)/.test(String(timer.fn)));
  const polls = () => sent.filter((message) => message.method === 'tools/call' && message.params.name === 'get_call_update');
  const answer = async (message, result) => {
    deliver({ jsonrpc: '2.0', id: message.id, result });
    await settle();
  };
  return {
    main, root, head, sent, timers, deliver, deliverFrom, settle, clock, fire, setHidden, setOnScreen, advance,
    pending, polls, answer,
  };
}

// Each copy of a call a test hands the card is newer than the one before.
let versions = 100;
const newer = (record) => ({ ...record, version: ++versions });
const toolResult = (record) => ({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { content: [], structuredContent: record } });

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
  card.deliver(toolResult({ id: ID, version: 2, status: 'ringing', brief: { to: '+15550100', contact: { name } } }));
  // What the host handed over may be old: the card says it is checking, and asks the server at
  // once, through the bridge, with the version it has.
  assert.match(card.main.textContent, /Checking…/);
  assert.doesNotMatch(card.main.textContent, /Ringing|Starting/);
  assert.ok(card.main.textContent.includes(`Call to ${name}`));
  assert.ok(card.main.all().every((element) => element.tagName !== 'IMG'));
  const [first] = card.polls();
  assert.deepEqual(first.params, { name: 'get_call_update', arguments: { callId: ID, ifVersion: 2 } });
  await card.answer(first, { content: [], structuredContent: { callId: ID, status: 'ringing', version: 2, unchanged: true, nextPollMs: 3000 } });
  assert.match(card.main.textContent, /Ringing/);
  // Answered requests leave no timeouts behind: only the next ask is pending, at the ringing pace.
  assert.deepEqual(card.timers.filter((timer) => timer.live).map((timer) => timer.ms), [3000]);
  await card.advance(3000);
  const second = card.polls()[1];
  assert.deepEqual(second.params.arguments, { callId: ID, ifVersion: 2 });
  await card.answer(second, { content: [], structuredContent: { ...DONE, version: 9 } });
  const shown = card.main.textContent;
  for (const part of ['Done', 'Got it done', 'Transcript · 2', 'Open in Smitline']) {
    assert.ok(shown.includes(part), part);
  }
  for (const part of ['Cost', '$0.66', '$0.20/min']) assert.ok(!shown.includes(part), part);
  const [audio] = card.main.all().filter((element) => element.tagName === 'AUDIO');
  assert.equal(audio.src, 'https://app.example.com/r/abc');
  assert.equal(card.timers.filter((timer) => timer.live).length, 0); // done: no more asks
  await card.advance(60 * 60 * 1000);
  assert.equal(card.polls().length, 2);

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
    id: ID, version: 3, status: 'in_progress', brief: { to: '+15550100' },
    requests: [{ id: 'r1', state: 'open', text: 'Should I bring anything?', askedBy: 'Sam' }],
  };
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: live } });
  await card.answer(card.polls()[0], { structuredContent: { callId: ID, status: 'in_progress', version: 3, unchanged: true } });
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
  assert.deepEqual([again.method, again.params.name], ['tools/call', 'get_call_update']);
  const closed = { ...live, version: 4, requests: [{ ...live.requests[0], state: 'done' }] };
  card.deliver({ jsonrpc: '2.0', id: again.id, result: { content: [], structuredContent: closed } });
  await card.settle();
  assert.equal(box('r1'), undefined); // the question has gone; what the user said stays
  assert.ok(card.main.textContent.includes('Sam askedShould I bring anything?You repliedJust yourself'));
  assert.ok(!card.main.textContent.includes('Answered for you')); // the user's own answer, said once
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
  assert.ok(!card.main.textContent.includes('You told the call')); // only notes that went
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

test('a live call shows its last line and how many; an ended one the transcript, which stays open', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const live = { id: ID, status: 'in_progress', brief: { to: '+15550100', contact: { name: 'Sam' } },
    transcript: [{ speaker: 'agent', text: 'Hi Sam' }, { speaker: 'other', text: 'Hello?' }], transcriptCount: 12 };
  card.deliver(toolResult(newer(live)));
  assert.equal(card.main.querySelectorAll('details').length, 0);
  assert.ok(card.main.textContent.includes('Transcript · 12Sam: Hello?'));
  assert.ok(!card.main.textContent.includes('Hi Sam'));
  const ended = { ...live, status: 'completed', transcript: undefined, transcriptCount: undefined,
    result: { outcome: 'achieved', transcript: [{ speaker: 'agent', text: 'Hi Sam' }, { speaker: 'other', text: 'Hello?' }] } };
  card.deliver(toolResult(newer(ended)));
  const details = () => card.main.querySelectorAll('details')[0];
  assert.ok(details().textContent.startsWith('Transcript · 2Smitline: Hi SamSam: Hello?'));
  assert.equal(details().open, false);
  details().open = true;
  details().listeners.toggle[0]();
  card.deliver(toolResult(newer(ended)));
  assert.equal(details().open, true);
  // A saved copy with only the last lines says so.
  card.deliver(toolResult(newer({ ...ended, transcript: [{ speaker: 'other', text: 'Bye' }], transcriptCount: 30 })));
  assert.ok(details().textContent.startsWith('Transcript · 30The last 1:Sam: Bye'));
});

test('a question closed elsewhere while the user types says so, and keeps what they typed', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const ask = (id, text) => ({ id, state: 'open', text, askedBy: 'Sam', thread: [{ from: 'call', type: 'ask', text }] });
  const live = { id: ID, status: 'in_progress', brief: { to: '+15550100' }, requests: [ask('r1', 'Should I bring anything?')] };
  const show = (record) => card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: newer(record) } });
  const box = (key) => card.main.querySelectorAll('textarea').find((item) => item.dataset.key === key);
  show(live);
  box('r1').value = 'Just yours';
  box('r1').listeners.input[0]();

  // The agent answers the same question before the user sends theirs.
  const answered = { ...ask('r1', 'Should I bring anything?'), state: 'done' };
  answered.thread = [...answered.thread, { from: 'agent', type: 'done', text: 'Nothing, just yourself.' }];
  show({ ...live, requests: [answered, ask('r2', 'Is 5 fine?')] });
  assert.ok(card.main.textContent.includes('Sam askedShould I bring anything?Answered for youNothing, just yourself.'));
  assert.equal(box('r1'), undefined);
  assert.equal(box('r2').value, ''); // the next question starts empty: the text was for the first one

  // The person drops the next question; with none open, the typed text waits in "Tell the call".
  show({ ...live, requests: [answered, { ...ask('r2', 'Is 5 fine?'), state: 'canceled' }] });
  assert.ok(card.main.textContent.includes('Sam askedIs 5 fine?Sam dropped the question'));
  assert.equal(box('tell').value, 'Just yours');
  assert.ok(card.main.textContent.includes('Your reply, kept. Send it as your answer.'));
  show({ ...live, requests: [answered, { ...ask('r2', 'Is 5 fine?'), state: 'canceled' }] });
  assert.equal(card.main.textContent.split('Answered for you').length, 2); // each question once
});

test('kept text goes to the call as the user’s own late answer to the question', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const ask = (id, text) => ({ id, state: 'open', text, askedBy: 'Sam', thread: [{ from: 'call', type: 'ask', text }] });
  const live = { id: ID, status: 'in_progress', brief: { to: '+15550100' }, requests: [ask('r1', 'Should I bring anything?')] };
  const show = (record) => card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: newer(record) } });
  const box = (key) => card.main.querySelectorAll('textarea').find((item) => item.dataset.key === key);
  const type = (key, words) => { box(key).value = words; box(key).listeners.input[0](); };
  const submit = (key) => {
    const form = card.main.querySelectorAll('form').find((item) => item.children[0].dataset.key === key);
    form.listeners.submit[0]({ preventDefault() {} });
  };
  show(live);
  type('r1', 'Just yourself, see you at 5');
  const answered = { ...ask('r1', 'Should I bring anything?'), state: 'done' };
  answered.thread = [...answered.thread, { from: 'agent', type: 'done', text: 'Nothing needed.' }];
  show({ ...live, requests: [answered] });
  assert.equal(box('tell').value, 'Just yourself, see you at 5'); // the box shows only what the user typed

  submit('tell');
  const sent = card.sent.at(-1);
  assert.deepEqual(sent.params, { name: 'send_call_instruction', arguments: { callId: ID, text:
    'The user’s own answer, sent late, to Sam’s question “Should I bring anything?”, already answered with '
    + '“Nothing needed.”. Pass it on at the next pause as their answer, saying it just came from them; where it '
    + 'differs from the earlier answer, theirs counts: Just yourself, see you at 5' } });
  card.deliver({ jsonrpc: '2.0', id: sent.id, result: { content: [], isError: false } });
  await card.settle();

  assert.ok(card.main.textContent.includes('Answered for youNothing needed.You, laterJust yourself, see you at 5'));

  type('tell', 'We are 10 minutes late'); // the next note is just a note
  submit('tell');
  assert.equal(card.sent.at(-1).params.arguments.text, 'We are 10 minutes late');
  card.deliver({ jsonrpc: '2.0', id: card.sent.at(-1).id, result: { content: [], isError: false } });
  await card.settle();
  type('tell', 'And bring the cake');
  submit('tell');
  card.deliver({ jsonrpc: '2.0', id: card.sent.at(-1).id, result: { content: [], isError: false } });
  await card.settle();
  // Every note stays on the card, in the order sent; notes in a row share one label, and the
  // note itself is the sign it went.
  assert.ok(card.main.textContent.includes('You, laterJust yourself, see you at 5'
    + 'You told the callWe are 10 minutes lateAnd bring the cake'));
  assert.equal(card.main.textContent.split('You told the call').length, 2);
  assert.ok(!card.main.textContent.includes('Sent to the call.'));
});

test('kept text the user clears, or a dropped question, frames the note to match', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const ask = (id, text) => ({ id, state: 'open', text, askedBy: 'Sam', thread: [{ from: 'call', type: 'ask', text }] });
  const live = { id: ID, status: 'in_progress', brief: { to: '+15550100' }, requests: [ask('r1', 'Is 5 fine?')] };
  const show = (record) => card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: { structuredContent: newer(record) } });
  const box = (key) => card.main.querySelectorAll('textarea').find((item) => item.dataset.key === key);
  const type = (key, words) => { box(key).value = words; box(key).listeners.input[0](); };
  const submit = (key) => {
    const form = card.main.querySelectorAll('form').find((item) => item.children[0].dataset.key === key);
    form.listeners.submit[0]({ preventDefault() {} });
  };
  show(live);
  type('r1', 'Make it 6');
  show({ ...live, requests: [{ ...ask('r1', 'Is 5 fine?'), state: 'canceled' }] });
  submit('tell');
  assert.ok(card.sent.at(-1).params.arguments.text.startsWith(
    'The user’s own answer, sent late, to Sam’s question “Is 5 fine?”, which Sam then dropped. Pass it on'));
  card.deliver({ jsonrpc: '2.0', id: card.sent.at(-1).id, result: { isError: true, content: [] } });
  await card.settle();

  type('tell', ''); // the user clears the kept text and writes something else
  type('tell', 'Say hi to Sam');
  submit('tell');
  assert.equal(card.sent.at(-1).params.arguments.text, 'Say hi to Sam');
});

test('the card says nothing about questions it never showed, or that the user answered', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const done = (id, text) => ({ id, state: 'done', text, thread: [{ from: 'agent', type: 'done', text: 'Done.' }] });
  card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: {
    structuredContent: { id: ID, status: 'in_progress', requests: [done('r1', 'Before the card opened?')] },
  } });
  assert.ok(!card.main.textContent.includes('Answered for you'));
  assert.deepEqual(callCardView({ id: ID, status: 'completed', requests: [done('r1', 'x')] }).closed, []);
  assert.deepEqual(callCardView({ id: ID, status: 'in_progress', requests: [done('r1', 'x')] }).closed,
    [{ id: 'r1', text: 'x', askedBy: '', canceled: false, answer: 'Done.' }]);
});

test('the card keeps the last three entries of the thread, and shows the rest on a click', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const ask = (id, text, state = 'open') => ({ id, state, text, askedBy: 'Sam',
    thread: [{ from: 'call', type: 'ask', text }, ...(state === 'done' ? [{ from: 'agent', type: 'done', text: 'Answer ' + id }] : [])] });
  const show = (requests) => card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: {
    structuredContent: newer({ id: ID, status: 'in_progress', brief: { to: '+15550100' }, requests }) } });
  const ids = ['r1', 'r2', 'r3', 'r4', 'r5'];
  const done = [];
  for (const id of ids) { // each question shows, then the agent answers it
    show([...done, ask(id, 'Question ' + id)]);
    done.push(ask(id, 'Question ' + id, 'done'));
  }
  show(done);
  const text = () => card.main.textContent;
  assert.ok(text().includes('Show 2 earlier'));
  assert.ok(!text().includes('Question r2') && text().includes('Question r3') && text().includes('Question r5'));
  const more = card.main.querySelectorAll('button').find((item) => item.textContent === 'Show 2 earlier');
  more.listeners.click[0]();
  assert.ok(text().includes('Question r1') && !text().includes('earlier'));
  show(done); // and it stays open as the card follows the call
  assert.ok(text().includes('Question r1'));
});

test('messages for the user join the thread as said, never as a question to answer', async () => {
  const card = loadCard();
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, result: {} });
  await card.settle();
  const message = (id, text, extra = {}) => ({ id, kind: 'message', state: 'noted', text, askedBy: 'Sam',
    thread: [{ from: 'call', type: 'ask', text }], ...extra });
  const show = (requests) => card.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: {
    structuredContent: newer({ id: ID, status: 'in_progress', brief: { to: '+15550100' }, requests }) } });
  const box = (key) => card.main.querySelectorAll('textarea').find((item) => item.dataset.key === key);
  show([message('r1', 'Thanks for coming to dinner')]);
  assert.ok(card.main.textContent.includes('Sam saidThanks for coming to dinner'));
  assert.ok(!card.main.textContent.includes('Sam asks'));
  assert.ok(box('tell')); // the box stays "Tell the call"
  assert.equal(box('r1'), undefined);
  // The user's reply, sent through their agent, shows under it.
  show([message('r1', 'Thanks for coming to dinner', { state: 'done',
    thread: [{ from: 'call', type: 'ask', text: 'x' }, { from: 'agent', type: 'done', text: 'So glad we came' }] })]);
  assert.ok(card.main.textContent.includes('Sam saidThanks for coming to dinnerYou repliedSo glad we came'));
  assert.equal(card.main.textContent.split('Sam said').length, 2);
  assert.deepEqual(callCardView({ id: ID, status: 'in_progress', requests: [message('r2', 'Hi')] }).messages,
    [{ id: 'r2', text: 'Hi', askedBy: 'Sam', reply: '' }]);
  assert.deepEqual(callCardView({ id: ID, status: 'in_progress', requests: [message('r2', 'Hi')] }).requests, []);
});

// Following the call: the card takes only newer copies, paces itself, and stops.

const LIVE = { id: ID, channel: 'phone', status: 'in_progress', brief: { to: '+15550100', contact: { name: 'Bawarchi' } },
  answeredAt: '2026-11-18T14:59:00Z', transcript: [{ speaker: 'other', text: 'Hello?' }], transcriptCount: 1 };
const QUEUED = { id: ID, version: 1, channel: 'phone', status: 'queued', brief: { to: '+15550100', contact: { name: 'Bawarchi' } } };
const unchanged = (version, status = 'in_progress', nextPollMs = 5000) => ({ structuredContent: { callId: ID, status, version, unchanged: true, nextPollMs } });

// A ChatGPT-like window.openai that records what the card saves.
function fakeOpenai(extra = {}) {
  const saved = [];
  return { saved, theme: 'light', setWidgetState(value) { saved.push(JSON.parse(JSON.stringify(value))); }, ...extra };
}

async function following(record = { ...LIVE, version: 5 }) {
  const card = loadCard();
  await card.answer(card.sent[0], {});
  card.deliver(toolResult(record));
  return card;
}

test('an older copy never replaces a newer one, and the status never moves back', async () => {
  const card = await following();
  await card.answer(card.polls()[0], { structuredContent: { ...LIVE, version: 8, transcript: [{ speaker: 'other', text: 'Yes, 7pm.' }] } });
  assert.match(card.main.textContent, /On the call/);
  assert.match(card.main.textContent, /Yes, 7pm\./);
  // The snapshot that made the card, replayed by the host: older, and behind.
  card.deliver(toolResult(QUEUED));
  card.deliver(toolResult({ ...LIVE, version: 8, transcript: [{ speaker: 'other', text: 'Old line' }] })); // same version
  card.deliver(toolResult({ ...LIVE, transcript: [{ speaker: 'other', text: 'No version' }] })); // counts as 0
  assert.match(card.main.textContent, /On the call/);
  assert.match(card.main.textContent, /Yes, 7pm\./);
  assert.doesNotMatch(card.main.textContent, /Starting|Old line|No version/);
  // A higher version whose status is behind is not taken either.
  card.deliver(toolResult({ ...LIVE, version: 20, status: 'ringing' }));
  assert.match(card.main.textContent, /On the call/);
  // Nor is another call's.
  card.deliver(toolResult({ ...LIVE, id: 'call-fedcba9876543210', version: 30, brief: { to: '+15559999' } }));
  assert.doesNotMatch(card.main.textContent, /15559999/);
  card.deliver(toolResult({ ...LIVE, version: 21, status: 'summarizing' }));
  assert.match(card.main.textContent, /Wrapping up/);
});

test('"unchanged" re-arms at the larger of nextPollMs and the status pace', async () => {
  const card = await following();
  const before = card.main.children[0];
  await card.answer(card.polls()[0], unchanged(5, 'in_progress', 9000));
  assert.deepEqual(card.pending().map((timer) => timer.ms), [9000]);
  const drawn = card.main.children[0];
  await card.advance(9000);
  await card.answer(card.polls()[1], unchanged(5, 'in_progress', 1000)); // never faster than the pace
  assert.deepEqual(card.pending().map((timer) => timer.ms), [5000]);
  assert.equal(card.main.children[0], drawn); // nothing new: nothing redrawn
  assert.notEqual(before, drawn); // the first answer took "Checking…" away
  await card.advance(5000);
  await card.answer(card.polls()[2], { structuredContent: { callId: ID, status: 'in_progress', version: 5, unchanged: true } });
  assert.deepEqual(card.pending().map((timer) => timer.ms), [5000]); // no hint: the pace
  await card.advance(4999);
  assert.equal(card.polls().length, 3);
  await card.advance(1);
  assert.equal(card.polls().length, 4);
  await card.answer(card.polls()[3], { structuredContent: { ...LIVE, version: 6, status: 'ringing', nextPollMs: 3000 } });
  assert.match(card.main.textContent, /On the call/); // a status behind is not taken
});

test('an ended call stops the card and is saved; set_globals with an old toolOutput changes nothing', async () => {
  const openai = fakeOpenai({ toolOutput: QUEUED });
  const card = loadCard({ openai });
  assert.match(card.main.textContent, /Checking…/);
  await card.answer(card.sent[0], {});
  assert.equal(card.polls().length, 1); // asked at once
  await card.answer(card.polls()[0], { structuredContent: { ...DONE, version: 40, nextPollMs: 0 } });
  assert.match(card.main.textContent, /Got it done/);
  assert.equal(card.pending().length, 0);
  const last = openai.saved.at(-1);
  assert.deepEqual([last.callId, last.version, last.status], [ID, 40, 'completed']);
  assert.equal(last.record.result.outcome, 'achieved');
  assert.deepEqual(last.record.transcript, [{ speaker: 'other', text: 'Sure.' }]);
  assert.equal(last.record.transcriptCount, 2);
  assert.ok(JSON.stringify(last).length < 1200, JSON.stringify(last).length);
  // ChatGPT fires set_globals on every change it makes, with the toolOutput that made the card.
  const drawn = card.main.children[0];
  const saves = openai.saved.length;
  for (let index = 0; index < 3; index += 1) card.fire('openai:set_globals');
  assert.equal(card.main.children[0], drawn);
  assert.equal(card.polls().length, 1);
  assert.equal(card.pending().length, 0);
  assert.equal(openai.saved.length, saves);
  await card.advance(60 * 60 * 1000);
  assert.equal(card.polls().length, 1);
});

test('a card opened again with a saved ended call shows how it ended and asks for nothing', async () => {
  const kept = { callId: ID, version: 40, status: 'completed', record: { ...DONE, version: 40, transcript: undefined, transcriptCount: 2 } };
  const card = loadCard({ openai: fakeOpenai({ toolOutput: QUEUED, widgetState: kept }) });
  assert.match(card.main.textContent, /Done/);
  assert.match(card.main.textContent, /Got it done/);
  assert.doesNotMatch(card.main.textContent, /Starting|Checking/);
  // Claude replays the result that made the card too: still nothing to ask.
  card.deliver(toolResult(QUEUED));
  await card.answer(card.sent[0], {});
  await card.advance(60 * 60 * 1000);
  assert.equal(card.polls().length, 0);
  assert.equal(card.sent.filter((message) => message.method === 'tools/call').length, 0);
  // A saved state for another call is not this card's.
  const other = loadCard({ openai: fakeOpenai({ toolOutput: QUEUED, widgetState: { ...kept, callId: 'call-fedcba9876543210',
    record: { ...kept.record, id: 'call-fedcba9876543210' } } }) });
  assert.match(other.main.textContent, /Checking…/);
  await other.answer(other.sent[0], {});
  assert.equal(other.polls().length, 1);
});

test('a card opened again while the call runs shows what it has, and asks once at once', async () => {
  const kept = { callId: ID, version: 12, status: 'in_progress', record: { ...LIVE, version: 12 } };
  const card = loadCard({ openai: fakeOpenai({ toolOutput: {}, widgetState: kept }) }); // rehydrated: toolOutput {}
  assert.equal(card.sent[0].method, 'ui/initialize'); // first, before any ask
  await card.answer(card.sent[0], {});
  assert.match(card.main.textContent, /Checking…/);
  assert.match(card.main.textContent, /Hello\?/);
  assert.deepEqual(card.polls().map((message) => message.params.arguments), [{ callId: ID, ifVersion: 12 }]);
  await card.answer(card.polls()[0], unchanged(12));
  assert.match(card.main.textContent, /On the call/);
  assert.equal(card.polls().length, 1);
  // A result that came without structuredContent still names the call: in _meta, or on the first line.
  for (const result of [{ content: [{ type: 'text', text: 'x' }], _meta: { callId: ID } },
    { content: [{ type: 'text', text: `${ID}\nnot json` }] }]) {
    const bare = loadCard();
    await bare.answer(bare.sent[0], {});
    bare.deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: result });
    await bare.settle();
    assert.deepEqual(bare.polls().map((message) => message.params.arguments), [{ callId: ID }]);
  }
});

test('the card asks nothing while nobody can see it, and asks once when it is seen', async () => {
  const card = await following();
  await card.answer(card.polls()[0], unchanged(5));
  card.setHidden(true);
  await card.advance(60_000);
  assert.equal(card.polls().length, 1);
  assert.equal(card.pending().length, 0);
  card.setHidden(false);
  assert.equal(card.polls().length, 2);
  await card.answer(card.polls()[1], unchanged(5));
  card.fire('focus'); // seen all along: nothing extra
  assert.equal(card.polls().length, 2);
  // Scrolled out of sight is the same.
  card.setOnScreen(false);
  await card.advance(60_000);
  assert.equal(card.polls().length, 2);
  card.setOnScreen(true);
  assert.equal(card.polls().length, 3);
  await card.answer(card.polls()[2], unchanged(5));
  card.setOnScreen(false);
  await card.advance(5000);
  card.fire('focus'); // still out of sight
  assert.equal(card.polls().length, 3);
});

test('failures keep the card, back off, and honor "try again in N s"', async () => {
  const card = await following();
  await card.answer(card.polls()[0], { structuredContent: { ...LIVE, version: 6 } });
  const shown = () => card.main.textContent;
  await card.advance(5000);
  // The service refuses for a while: the bridge passes its message on as an error.
  card.deliver({ jsonrpc: '2.0', id: card.polls()[1].id, error: { code: -32000, message: 'Too many requests: try again in 7 s.' } });
  await card.settle();
  assert.match(shown(), /Reconnecting…/);
  assert.match(shown(), /Hello\?/); // the last good copy stays
  assert.doesNotMatch(shown(), /Loading/);
  assert.deepEqual(card.pending().map((timer) => timer.ms), [7000]);
  const waits = [];
  for (let index = 2; index < 7; index += 1) {
    await card.advance(card.pending()[0].ms);
    await card.answer(card.polls()[index], { isError: true, content: [{ type: 'text', text: '{"code":"startup","message":"unavailable"}' }] });
    waits.push(card.pending()[0].ms);
  }
  assert.deepEqual(waits, [10000, 20000, 40000, 60000, 60000]);
  // Two minutes on: it says so, with the time of the last good copy, and offers to try now.
  assert.match(shown(), /Couldn’t refresh · last updated/);
  const refresh = card.main.querySelectorAll('button').find((button) => button.textContent === 'Refresh');
  refresh.listeners.click[0]();
  assert.equal(card.polls().length, 8);
  await card.answer(card.polls()[7], unchanged(6));
  assert.doesNotMatch(shown(), /Reconnecting|Couldn’t/);
  assert.deepEqual(card.pending().map((timer) => timer.ms), [5000]);
  // A bridge that never answers counts as a failure too, once its 20 s are up.
  await card.advance(5000);
  await card.advance(20000);
  assert.match(shown(), /Reconnecting…/);
});

test('after 30 minutes the card pauses, and Refresh follows the call for another 30', async () => {
  const card = await following();
  let asked = 0;
  for (;;) {
    const poll = card.polls()[asked];
    if (!poll) break;
    asked += 1;
    await card.answer(poll, unchanged(5));
    await card.advance(5000);
  }
  assert.ok(asked >= 359 && asked <= 361, String(asked)); // every 5 s for 30 minutes
  assert.match(card.main.textContent, /Paused/);
  assert.equal(card.pending().length, 0);
  await card.advance(60 * 60 * 1000);
  assert.equal(card.polls().length, asked);
  const refresh = card.main.querySelectorAll('button').find((button) => button.textContent === 'Refresh');
  refresh.listeners.click[0]();
  assert.equal(card.polls().length, asked + 1);
  await card.answer(card.polls()[asked], unchanged(5));
  assert.doesNotMatch(card.main.textContent, /Paused/);
  assert.deepEqual(card.pending().map((timer) => timer.ms), [5000]);
});

test('once the host closes the card, nothing pending acts', async () => {
  const card = await following();
  const poll = card.polls()[0];
  card.deliver({ jsonrpc: '2.0', id: 7, method: 'ui/resource-teardown', params: {} });
  const drawn = card.main.textContent;
  await card.answer(poll, { structuredContent: { ...DONE, version: 50 } });
  assert.equal(card.main.textContent, drawn);
  assert.equal(card.pending().length, 0);
  await card.advance(60 * 60 * 1000);
  assert.equal(card.polls().length, 1);
  // The same when the page goes away (ChatGPT's old card, on the next turn).
  const old = await following();
  await old.answer(old.polls()[0], unchanged(5));
  old.fire('pagehide');
  await old.advance(60 * 60 * 1000);
  assert.equal(old.polls().length, 1);
});

test('without the MCP Apps bridge the card asks through window.openai.callTool', async () => {
  const asked = [];
  const openai = fakeOpenai({
    toolOutput: { ...LIVE, version: 5 },
    async callTool(name, args) { asked.push([name, JSON.parse(JSON.stringify(args))]); return unchanged(5); },
  });
  const card = loadCard({ openai });
  assert.equal(asked.length, 0); // not before the host has answered ui/initialize
  card.deliver({ jsonrpc: '2.0', id: card.sent[0].id, error: { code: -32601, message: 'no' } }); // not an MCP Apps host
  await card.settle();
  assert.deepEqual(asked, [['get_call_update', { callId: ID, ifVersion: 5 }]]);
  await card.advance(5000);
  assert.equal(asked.length, 2);
  assert.equal(card.polls().length, 0); // never through the bridge
});

test('the view cuts nothing the card needs from a get_call_update answer', async () => {
  const { callCardUpdate } = await import('../src/call-card.mjs');
  const update = callCardUpdate({ ...DONE, version: 3, transcript: undefined, result: { ...DONE.result, transcript: DONE.transcript } });
  const view = callCardView(update);
  assert.deepEqual(view, callCardView({ ...DONE, version: 3 }));
});
