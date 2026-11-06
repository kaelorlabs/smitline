import assert from 'node:assert/strict';
import test from 'node:test';
import { CALLS_INSTRUCTIONS, HOW_TO_TOPICS, createMcpSession } from '../src/session.mjs';

async function call(session, method, params, id = 1) {
  return session.dispatch({ jsonrpc: '2.0', id, method, params });
}

async function tools() {
  const session = createMcpSession({ colleague: {}, log() {} });
  const listed = await call(session, 'tools/list', {});
  return Object.fromEntries(listed.result.tools.map((tool) => [tool.name, tool]));
}

test('how_to_smitline is a read-only tool with a title and one optional topic', async () => {
  const tool = (await tools()).how_to_smitline;
  assert.equal(tool.title, 'How to use Smitline');
  assert.deepEqual(tool.annotations, { readOnlyHint: true, openWorldHint: false });
  assert.deepEqual(tool.inputSchema.required, undefined);
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.deepEqual(Object.keys(tool.inputSchema.properties), ['topic']);
  assert.deepEqual(tool.inputSchema.properties.topic.enum,
    ['overview', 'using', 'following', 'pricing', 'account', 'troubleshooting', 'rules']);
  assert.deepEqual(tool.inputSchema.properties.topic.enum, [...HOW_TO_TOPICS]);
  for (const topic of HOW_TO_TOPICS) assert.match(tool.inputSchema.properties.topic.description, new RegExp(`\\b${topic}\\b`));
  assert.match(tool.inputSchema.properties.topic.description, /overview \(the default\)/);
});

test('its description says when it helps, plainly and briefly', async () => {
  const { description } = (await tools()).how_to_smitline;
  assert.match(description, /when the user asks about Smitline, its prices or their account, or when a call was refused/);
  assert.ok(description.length < 500, description.length);
  // Factual: no selling words, and it names no other app (directory rules).
  for (const word of [/\bbest\b/i, /\bfree\b/i, /\bupgrade\b/i, /\bpro\b/i, /\bdiscount/i, /ChatGPT|Claude|OpenAI|Anthropic/]) {
    assert.doesNotMatch(description, word);
  }
});

test('the server instructions point to it with the same words', () => {
  assert.match(CALLS_INSTRUCTIONS, /When the user asks about Smitline, its prices or their account, or a call was refused, how_to_smitline has the facts/);
});

test('it reads the topic from the daemon, overview when none is given', async () => {
  const asked = [];
  const colleague = {
    async howTo(...args) {
      asked.push(args);
      return { topic: args[0] || 'overview', text: 'Smitline makes phone calls.', links: [] };
    },
  };
  const session = createMcpSession({ colleague, log() {} });
  const overview = await call(session, 'tools/call', { name: 'how_to_smitline', arguments: {} });
  assert.equal(overview.result.isError, false);
  assert.deepEqual(overview.result.structuredContent, { topic: 'overview', text: 'Smitline makes phone calls.', links: [] });
  // Not a call record: no next step is added.
  assert.equal(overview.result.structuredContent.next, undefined);
  await call(session, 'tools/call', { name: 'how_to_smitline', arguments: { topic: 'account' } });
  assert.deepEqual(asked, [[undefined], ['account']]);
});

test('an unknown topic is refused before the daemon is asked', async () => {
  const asked = [];
  const session = createMcpSession({ colleague: { async howTo(topic) { asked.push(topic); return {}; } }, log() {} });
  for (const topic of ['prices', 'ACCOUNT', '', 3]) {
    const answer = await call(session, 'tools/call', { name: 'how_to_smitline', arguments: { topic } });
    assert.equal(answer.result.isError, true);
    assert.equal(answer.result.structuredContent.code, 'validation');
    assert.match(answer.result.structuredContent.message, /topic must be one of: overview, using, following, pricing, account, troubleshooting, rules/);
  }
  assert.deepEqual(asked, []);
});

test('the modern protocol serves it too', async () => {
  const colleague = { async howTo(topic) { return { topic, text: 'Rules.', links: [] }; } };
  const session = createMcpSession({ colleague, log() {} });
  const answer = await session.dispatchModern({
    jsonrpc: '2.0', id: 9, method: 'tools/call', params: { name: 'how_to_smitline', arguments: { topic: 'rules' } },
  });
  assert.equal(answer.result.resultType, 'complete');
  assert.deepEqual(answer.result.structuredContent, { topic: 'rules', text: 'Rules.', links: [] });
});
