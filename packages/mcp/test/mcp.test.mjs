import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { ColleagueError } from '../../sdk-typescript/src/index.mjs';
import { CALLS_INSTRUCTIONS, createMcpSession, TOOL_DEFINITIONS } from '../src/session.mjs';
import { startStdioServer } from '../src/server.mjs';

const serverPath = fileURLToPath(new URL('../src/server.mjs', import.meta.url));

function createFakeColleague() {
  return {
    async startCall(brief) { return { id: 'call-0123456789abcdef', status: 'queued', brief }; },
    async listVoices() { return { default: 'marin', voices: ['marin'] }; },
  };
}

async function call(session, method, params, id = 1) {
  return session.dispatch({ jsonrpc: '2.0', id, method, params });
}

test('initialize describes calls and meetings, and lists only the call tools', async () => {
  const session = createMcpSession({ colleague: createFakeColleague(), log() {} });
  const init = await call(session, 'initialize', {
    protocolVersion: '2025-06-18',
    clientInfo: { name: 'test', version: '1' },
    capabilities: {},
  });
  assert.equal(init.result.serverInfo.name, 'smitline');
  assert.equal(init.result.capabilities.extensions, undefined);
  assert.match(init.result.instructions, /start_call/);
  assert.match(init.result.instructions, /channel 'meeting'/);
  const listed = await call(session, 'tools/list', {});
  const names = listed.result.tools.map((tool) => tool.name);
  assert.deepEqual(names, [
    'start_call', 'check_call_brief', 'wait_for_call', 'get_call', 'list_calls',
    'save_call_note', 'list_contacts',
    'send_call_instruction', 'reply_to_call_request', 'end_call', 'transfer_call_to_me', 'list_voices',
    'get_profile', 'update_profile', 'how_to_smitline',
  ]);
  assert.equal(listed.result.tools.length, TOOL_DEFINITIONS.length);
  const startTool = listed.result.tools.find((tool) => tool.name === 'start_call');
  assert.deepEqual(startTool.inputSchema.properties.channel.enum, ['phone', 'meeting']);
  assert.equal(startTool.inputSchema.properties.agentSession, undefined);
});

test('removed meeting and coding tools are unknown', async () => {
  const session = createMcpSession({ colleague: createFakeColleague(), log() {} });
  for (const name of ['join_current_meeting', 'start_meeting', 'get_meeting_handoff', 'pair_runner']) {
    const result = await call(session, 'tools/call', { name, arguments: {} });
    assert.equal(result.result.isError, true);
    assert.match(result.result.structuredContent.message, /unknown tool/);
  }
  const tasks = await call(session, 'tasks/get', { taskId: 'task-1' });
  assert.equal(tasks.error.code, -32601);
});

test('a meeting is joined through start_call', async () => {
  const session = createMcpSession({ colleague: createFakeColleague(), log() {} });
  const brief = { channel: 'meeting', to: 'https://zoom.us/j/555', objective: 'Take notes on the roadmap review' };
  const started = await call(session, 'tools/call', { name: 'start_call', arguments: brief });
  assert.deepEqual(started.result.structuredContent.brief, brief);
});

test('notes for later calls, contacts, and list filters reach the SDK', async () => {
  const seen = [];
  const colleague = {
    async listCalls(limit, filters) { seen.push(['listCalls', limit, filters]); return []; },
    async saveCallNote(callId, text) { seen.push(['saveCallNote', callId, text]); return { id: callId }; },
    async listContacts() { seen.push(['listContacts']); return [{ number: '+14155550142' }]; },
  };
  const session = createMcpSession({ colleague, log() {} });
  await call(session, 'tools/call', { name: 'list_calls', arguments: { task: 'roof-quotes', channel: 'phone' } });
  await call(session, 'tools/call', { name: 'save_call_note', arguments: { callId: 'call-0123456789abcdef', text: 'Apex: $14,200.' } });
  const contacts = await call(session, 'tools/call', { name: 'list_contacts', arguments: {} });
  assert.deepEqual(contacts.result.structuredContent, { contacts: [{ number: '+14155550142' }] });
  assert.deepEqual(seen, [
    ['listCalls', 20, { channel: 'phone', task: 'roof-quotes' }],
    ['saveCallNote', 'call-0123456789abcdef', 'Apex: $14,200.'],
    ['listContacts'],
  ]);
  const empty = await call(session, 'tools/call', { name: 'save_call_note', arguments: { callId: 'call-0123456789abcdef', text: ' ' } });
  assert.equal(empty.result.isError, true);
  const start = TOOL_DEFINITIONS.find((tool) => tool.name === 'start_call');
  assert.deepEqual(Object.keys(start.inputSchema.properties.carryFrom.properties), ['task', 'contact', 'calls']);
});

test('a meeting brief can carry its dial-in, and start_call passes it on', async () => {
  const start = TOOL_DEFINITIONS.find((tool) => tool.name === 'start_call');
  const dialIn = start.inputSchema.properties.dialIn;
  assert.equal(dialIn.additionalProperties, false);
  assert.deepEqual(Object.keys(dialIn.properties).sort(), ['invite', 'keys', 'meetingId', 'number', 'passcode', 'platform']);
  assert.match(start.inputSchema.properties.to.description, /dial-in number/);
  // A dial-in number that is not Zoom's own keeps a phone call's hours, which afterHours lifts.
  assert.match(dialIn.description, /Zoom's own dial-in numbers can be dialed any time/);
  assert.match(start.inputSchema.properties.afterHours.description, /meetings with dialIn/);
  const briefs = [];
  const session = createMcpSession({ colleague: { async startCall(brief) { briefs.push(brief); return { id: 'call-0123456789abcdef', status: 'queued' }; } }, log() {} });
  const brief = { channel: 'meeting', objective: 'Take notes', dialIn: { invite: 'Meeting ID: 859 5128 9482\n+14388097799,,85951289482#' } };
  await call(session, 'tools/call', { name: 'start_call', arguments: brief });
  assert.deepEqual(briefs, [brief]);
});

test('agents are told to pass the dial-in up front for Google Meet, and check_call_brief warns without it', async () => {
  const start = TOOL_DEFINITIONS.find((tool) => tool.name === 'start_call');
  // Google often turns guest bots away: with dialIn, Smitline joins by phone by itself.
  assert.match(start.inputSchema.properties.dialIn.description, /For Google Meet, pass it whenever the invitation has a Join by phone number/);
  assert.match(start.inputSchema.properties.to.description, /Google Meet link, add dialIn too/);
  const check = TOOL_DEFINITIONS.find((tool) => tool.name === 'check_call_brief');
  assert.match(check.description, /warnings that would not stop it/);
  const warning = 'Google Meet often turns guest bots away: pass dialIn.';
  const session = createMcpSession({
    colleague: { async checkCall(brief) { return { ok: true, brief, problems: [], warnings: [warning], carried: 0 }; } },
    log() {},
  });
  const checked = await call(session, 'tools/call', {
    name: 'check_call_brief', arguments: { channel: 'meeting', to: 'https://meet.google.com/abc-defg-hij', objective: 'Take notes' },
  });
  assert.equal(checked.result.isError, false);
  assert.deepEqual(checked.result.structuredContent.warnings, [warning]);
  assert.equal(checked.result.structuredContent.ok, true);
});

test('a result that says why and what next reaches the agent as is', async () => {
  const result = {
    outcome: 'not_reached', summary: 'Google Meet did not let Smitline in as a guest.',
    reason: 'Google Meet did not let Smitline in as a guest.',
    hint: 'Ask the user for the Join by phone number and PIN from the invitation, if it has them, then start again with them as dialIn.',
    details: [], decisions: [], actionItems: [], openQuestions: [], transcript: [], durationSeconds: 0,
  };
  const session = createMcpSession({
    colleague: { async getCall(callId) { return { id: callId, channel: 'meeting', status: 'completed', result }; } },
    log() {},
  });
  const got = await call(session, 'tools/call', { name: 'get_call', arguments: { callId: 'call-0123456789abcdef' } });
  assert.equal(got.result.structuredContent.result.reason, result.reason);
  assert.equal(got.result.structuredContent.result.hint, result.hint);
});

test('startup errors are mapped without leaking tokens', async () => {
  const colleague = {
    async startCall() {
      throw new ColleagueError('Bearer secret-token-value rejected', { code: 'startup', status: 503 });
    },
  };
  const session = createMcpSession({ colleague, log() {} });
  const result = await call(session, 'tools/call', {
    name: 'start_call', arguments: { channel: 'phone', to: '+14155550142', objective: 'x' },
  });
  assert.equal(result.result.isError, true);
  assert.equal(result.result.structuredContent.code, 'startup');
  assert.ok(!JSON.stringify(result).includes('secret-token-value'));
});

test('stdio stdout is protocol frames only', async () => {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  let out = '';
  stdout.on('data', (chunk) => { out += chunk; });
  const { session } = startStdioServer({
    stdin,
    stdout,
    colleague: createFakeColleague(),
    installSignals: false,
    log() {},
  });
  stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } })}\n`);
  stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })}\n`);
  await new Promise((resolve) => setTimeout(resolve, 40));
  await session.shutdown();
  assert.ok(!out.includes('secret'));
  assert.ok(!out.includes('Bearer'));
  for (const line of out.split('\n').filter(Boolean)) {
    const parsed = JSON.parse(line);
    assert.equal(parsed.jsonrpc, '2.0');
  }
});

test('spawned process does not print logs on stdout', async () => {
  const child = spawn(process.execPath, [serverPath], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })}\n`);
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline && !(stdout.includes('"jsonrpc"'))) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  child.kill('SIGTERM');
  await new Promise((resolve) => child.on('close', resolve));
  assert.ok(stdout.includes('"jsonrpc":"2.0"') || stdout.includes('"jsonrpc": "2.0"'));
  for (const line of stdout.split('\n').filter(Boolean)) {
    const parsed = JSON.parse(line);
    assert.equal(parsed.jsonrpc, '2.0');
  }
  assert.ok(!stdout.toLowerCase().includes('daemon.auth'));
  assert.ok(!stdout.includes('Bearer'));
});

test('call tools map to the SDK and surface brief questions', async () => {
  const calls = [];
  const callId = 'call-0123456789abcdef';
  const colleague = {
    async startCall(brief) { calls.push(['start', brief]); return { id: callId, status: 'queued' }; },
    async checkCall(brief) { calls.push(['check', brief]); return { ok: true, problems: [] }; },
    async waitForCall(id, timeout, options) {
      calls.push(['wait', id, timeout, options]);
      return { id, status: 'completed', result: { outcome: 'achieved' } };
    },
    async getCall(id) { return { id, status: 'ringing' }; },
    async listCalls(limit) { calls.push(['list', limit]); return []; },
    async instructCall(id, text, options) { calls.push(['instruct', id, text, options]); return { delivered: true }; },
    async replyToRequest(id, requestId, reply) { calls.push(['reply', id, requestId, reply]); return { delivered: true }; },
    async endCall(id) { return { id, status: 'summarizing' }; },
    async transferCall() { return { transferred: true }; },
    async listVoices() { return { default: 'marin', voices: ['marin'] }; },
    async getProfile() { calls.push(['profile']); return { version: 1 }; },
    async updateProfile(update) { calls.push(['update-profile', update]); return { version: 1, ...update }; },
  };
  const session = createMcpSession({ colleague, log() {} });
  const brief = { channel: 'phone', to: '+14155550142', onBehalfOf: 'Robin', objective: 'Book a table' };
  const started = await call(session, 'tools/call', { name: 'start_call', arguments: brief });
  assert.equal(JSON.parse(started.result.content[0].text).id, callId);
  const waited = await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId } });
  assert.equal(JSON.parse(waited.result.content[0].text).result.outcome, 'achieved');
  // An agent's wait always follows live, from the start when it passes no cursor, so it counts
  // as following the call and a request wakes it.
  assert.deepEqual(calls[1], ['wait', callId, 50, { since: '', others: true }]);
  await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId, since: '', timeoutSeconds: 20 } });
  // Following live also watches the agent's other calls, unless it opts out.
  assert.deepEqual(calls.at(-1), ['wait', callId, 20, { since: '', others: true }]);
  await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId, since: '14' } });
  assert.deepEqual(calls.at(-1), ['wait', callId, 50, { since: '14', others: true }]);
  await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId, since: '14', others: false } });
  assert.deepEqual(calls.at(-1), ['wait', callId, 50, { since: '14', others: false }]);
  const badOthers = await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId, since: '', others: 'yes' } });
  assert.equal(badOthers.result.isError, true);
  const badSince = await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId, since: 'x' } });
  assert.equal(badSince.result.isError, true);
  await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId, only: 'requests' } });
  assert.deepEqual(calls.at(-1), ['wait', callId, 50, { since: '', only: 'requests', others: true }]);
  const badOnly = await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId, only: 'all' } });
  assert.equal(badOnly.result.isError, true);
  await call(session, 'tools/call', {
    name: 'reply_to_call_request',
    arguments: { callId, requestId: 'r2', type: 'status', text: 'Nov 3 or Nov 10?', announce: true },
  });
  assert.deepEqual(calls.at(-1), ['reply', callId, 'r2', { type: 'status', text: 'Nov 3 or Nov 10?', announce: true }]);
  await call(session, 'tools/call', {
    name: 'reply_to_call_request',
    arguments: { callId, requestId: 'r2', type: 'done', text: 'Added it.', ok: true, details: 'https://x/pr/81' },
  });
  assert.deepEqual(calls.at(-1), ['reply', callId, 'r2', { type: 'done', text: 'Added it.', ok: true, details: 'https://x/pr/81' }]);
  for (const bad of [{ requestId: 'x', type: 'done', text: 'a' }, { requestId: 'r1', type: 'maybe', text: 'a' },
    { requestId: 'r1', type: 'done', text: ' ' }]) {
    const refused = await call(session, 'tools/call', { name: 'reply_to_call_request', arguments: { callId, ...bad } });
    assert.equal(refused.result.isError, true, JSON.stringify(bad));
  }
  calls.splice(2);
  await call(session, 'tools/call', {
    name: 'send_call_instruction', arguments: { callId, text: 'Ask about parking' },
  });
  assert.deepEqual(calls[2], ['instruct', callId, 'Ask about parking', { silent: false }]);
  await call(session, 'tools/call', {
    name: 'send_call_instruction', arguments: { callId, text: 'He tried it yesterday', silent: true },
  });
  assert.deepEqual(calls[3], ['instruct', callId, 'He tried it yesterday', { silent: true }]);
  await call(session, 'tools/call', { name: 'get_profile', arguments: {} });
  const people = [{ name: 'Sam', relationship: 'close friend', phone: '+14155550143' }];
  await call(session, 'tools/call', { name: 'update_profile', arguments: { people } });
  assert.deepEqual(calls.slice(4), [['profile'], ['update-profile', { people }]]);
  const listed = await call(session, 'tools/list', {});
  const startTool = listed.result.tools.find((tool) => tool.name === 'start_call');
  assert.ok(startTool.inputSchema.properties.questions);
  assert.equal(startTool.inputSchema.properties.context.anyOf[1].properties.details.maxLength, 24000);
  calls.length = 0;
  const bad = await call(session, 'tools/call', { name: 'get_call', arguments: { callId: '../etc' } });
  assert.deepEqual(calls, []);
  assert.equal(bad.result.isError, true);

  colleague.startCall = async () => {
    const error = new ColleagueError('brief is missing objective', { code: 'brief_incomplete', status: 422 });
    error.details = { missing: [{ field: 'objective', question: 'What should the call achieve?' }] };
    throw error;
  };
  const incomplete = await call(session, 'tools/call', {
    name: 'start_call', arguments: { channel: 'phone', to: '+14155550142' },
  });
  assert.equal(incomplete.result.isError, true);
  const payload = JSON.parse(incomplete.result.content[0].text);
  assert.equal(payload.details.missing[0].question, 'What should the call achieve?');
});

test('start_call and wait_for_call results start with what to do next', async () => {
  const callId = 'call-0123456789abcdef';
  const otherId = 'call-fedcba9876543210';
  const answers = [];
  const colleague = {
    async startCall(brief) { return { id: callId, status: 'queued', brief }; },
    async waitForCall() {
      const answer = answers.shift();
      if (answer instanceof Error) throw answer;
      return answer;
    },
    async getCall(id) { return { id, status: 'in_progress' }; },
  };
  const session = createMcpSession({ colleague, log() {} });
  const tool = async (name, args) => {
    const reply = await call(session, 'tools/call', { name, arguments: args });
    const parsed = JSON.parse(reply.result.content[0].text);
    assert.deepEqual(parsed, reply.result.structuredContent);
    return parsed;
  };
  const wait = async (record) => {
    answers.push(record);
    const result = await tool('wait_for_call', { callId });
    assert.equal(Object.keys(result)[0], 'next');
    return result;
  };

  const started = await tool('start_call', { channel: 'phone', to: '+14155550142', objective: 'Book a table' });
  assert.equal(Object.keys(started)[0], 'next');
  assert.match(started.next, new RegExp(`wait_for_call with callId "${callId}" and since ""`));
  assert.match(started.next, /same turn/);
  assert.match(started.next, /until status is completed, failed, or canceled/);
  assert.equal(started.status, 'queued');

  const live = (extra = {}) => ({ id: callId, status: 'in_progress', live: { cursor: '41', transcript: [], requests: [], otherRequests: [], ...extra } });
  let result = await wait(live());
  assert.match(result.next, new RegExp(`call wait_for_call again with callId "${callId}" and since "41"`));
  assert.doesNotMatch(result.next, /reply_to_call_request/);
  assert.equal(result.live.cursor, '41');

  // A request comes first, answered with a reply and not repeated as an instruction.
  const asked = { type: 'ask', id: 'r1', kind: 'question', text: 'Who is listening?' };
  result = await wait({ ...live({ requests: [asked] }), requests: [{ id: 'r1', state: 'open' }] });
  assert.match(result.next, /^Answer each item in live\.requests with reply_to_call_request first/);
  assert.match(result.next, /don't also send it with send_call_instruction/);
  assert.match(result.next, /since "41"/);
  // Read again from the start, an answered request is not asked about again; their answer to
  // your question (add) on a request still being worked on is.
  result = await wait({ ...live({ requests: [asked] }), requests: [{ id: 'r1', state: 'done' }] });
  assert.doesNotMatch(result.next, /reply_to_call_request/);
  result = await wait({ ...live({ requests: [asked, { type: 'add', id: 'r1', text: 'Nov 3' }] }), requests: [{ id: 'r1', state: 'working' }] });
  assert.match(result.next, /^Answer each item in live\.requests/);
  result = await wait(live({ otherRequests: [{ callId: otherId, id: 'r2', text: 'Call Smith' }] }));
  assert.match(result.next, /live\.otherRequests \(on its own callId\)/);

  // On hold nothing is said for a long time: wait longer, and hear at once when someone picks up.
  result = await wait({ ...live(), brief: { maxHoldMinutes: 90 }, hold: { onHold: true, seconds: 300, times: 1 } });
  assert.match(result.next, /^The call is on hold: Smitline waits by itself/);
  assert.match(result.next, /timeoutSeconds 280/);
  assert.match(result.next, /90 minutes on hold/);
  assert.match(result.next, /since "41"/);
  result = await wait({ ...live(), hold: { onHold: false, seconds: 300, times: 1 } });
  assert.match(result.next, /^The call is in progress/);
  // A request still comes first while on hold.
  result = await wait({ ...live({ requests: [asked] }), requests: [{ id: 'r1', state: 'open' }], hold: { onHold: true } });
  assert.match(result.next, /^Answer each item in live\.requests/);

  result = await wait({ ...live(), status: 'summarizing' });
  assert.match(result.next, /result is being written/);
  result = await wait({ id: callId, status: 'completed', live: { cursor: '60' }, result: { outcome: 'achieved', openRequests: [] } });
  assert.match(result.next, /^The call has ended: tell the user the outcome/);
  assert.doesNotMatch(result.next, /openRequests/);
  result = await wait({ id: callId, status: 'completed', result: { outcome: 'partial', openRequests: [{ id: 'r3' }] } });
  assert.match(result.next, /finish result\.openRequests/);
  result = await wait({ id: callId, status: 'failed', error: 'The call could not be placed.' });
  assert.match(result.next, /^The call failed: tell the user what went wrong/);
  // A hint the daemon sent is replaced, still first.
  result = await wait({ next: 'stale', ...live() });
  assert.notEqual(result.next, 'stale');

  // Other tools, and failures, carry no next.
  assert.equal((await tool('get_call', { callId })).next, undefined);
  answers.push(Object.assign(new Error('call not found'), { code: 'not_found' }));
  const failed = await call(session, 'tools/call', { name: 'wait_for_call', arguments: { callId } });
  assert.equal(failed.result.isError, true);
  assert.equal(failed.result.structuredContent.next, undefined);
});

test('the tools tell agents to follow a call in the same turn, with canDo, answering with replies', () => {
  const tools = Object.fromEntries(TOOL_DEFINITIONS.map((tool) => [tool.name, tool]));
  const text = [CALLS_INSTRUCTIONS, JSON.stringify(TOOL_DEFINITIONS)].join('\n');
  // Without canDo nothing reaches the agent (call_service.note_request): no text may say otherwise.
  assert.doesNotMatch(text, /outside canDo/);
  for (const said of [CALLS_INSTRUCTIONS, tools.start_call.description, tools.wait_for_call.description]) {
    assert.match(said, /same turn/);
    assert.match(said, /completed, failed, or canceled/);
  }
  for (const said of [CALLS_INSTRUCTIONS, tools.start_call.description]) {
    assert.match(said, /canDo whenever you could help the people on the call, at least by answering questions about the user's work/);
    // Clicks: the read tools and the two used during a call are suggested for always allow, never start_call.
    assert.match(said, /wait_for_call, get_call and list_calls, and the two tools you use during a call \(send_call_instruction, reply_to_call_request\), to "Always allow"/);
    assert.match(said, /never suggest it for start_call/);
  }
  const canDo = tools.start_call.inputSchema.properties.canDo.description;
  assert.match(canDo, /Set it whenever you could help them/);
  assert.match(canDo, /Without it nothing they ask reaches you/);
  assert.match(tools.reply_to_call_request.description, /don't repeat it with send_call_instruction/);
  assert.match(tools.send_call_instruction.description, /Not for answering a request from the call/);
  assert.match(CALLS_INSTRUCTIONS, /don't repeat it with send_call_instruction/);
  assert.match(tools.wait_for_call.inputSchema.properties.since.description, /the default/);
});

test('each tool says honestly what it does: only lookups are read-only', () => {
  // Clients decide from these hints whether to ask the user, and directory reviews check that each
  // one fits. The in-call tools used to say read-only so they ran without asking; Claude Code also
  // repeats a read-only call after a dropped connection, so an instruction could be spoken twice.
  const hints = Object.fromEntries(TOOL_DEFINITIONS.map((tool) => [tool.name, tool.annotations]));
  const readOnly = Object.keys(hints).filter((name) => hints[name].readOnlyHint).sort();
  assert.deepEqual(readOnly, ['check_call_brief', 'get_call', 'get_profile', 'how_to_smitline', 'list_calls',
    'list_contacts', 'list_voices', 'wait_for_call']);
  // Ending, handing over and replacing are destructive; the other write tools only add.
  const destructive = Object.keys(hints).filter((name) => hints[name].destructiveHint).sort();
  assert.deepEqual(destructive, ['end_call', 'save_call_note', 'transfer_call_to_me']);
  for (const [name, hint] of Object.entries(hints)) {
    assert.equal(typeof hint.readOnlyHint, 'boolean', name);
    assert.equal(typeof hint.openWorldHint, 'boolean', name);
    if (!hint.readOnlyHint) {
      assert.equal(typeof hint.destructiveHint, 'boolean', name);
      assert.equal(typeof hint.idempotentHint, 'boolean', name);
    }
  }
  // Saying something twice, or a second goodbye, is not safe to repeat; a note saved twice is the same note.
  assert.equal(hints.send_call_instruction.idempotentHint, false);
  assert.equal(hints.reply_to_call_request.idempotentHint, false);
  assert.equal(hints.end_call.idempotentHint, false);
  assert.equal(hints.save_call_note.idempotentHint, true);
});

test('every tool has a title, in its annotations too', () => {
  // Claude's directory checks title among each tool's annotations (annotations.title); MCP's newer
  // top-level title is what most apps show. The two must say the same.
  for (const tool of TOOL_DEFINITIONS) {
    assert.equal(typeof tool.title, 'string', tool.name);
    assert.ok(tool.title.trim(), tool.name);
    assert.equal(tool.annotations.title, tool.title, tool.name);
  }
});
