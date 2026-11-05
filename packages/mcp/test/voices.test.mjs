import assert from 'node:assert/strict';
import test from 'node:test';
import { createMcpSession } from '../src/session.mjs';

async function call(session, method, params, id = 1) {
  return session.dispatch({ jsonrpc: '2.0', id, method, params });
}

test('the tools say what each default voice is for and that a call keeps its voice', async () => {
  const session = createMcpSession({ colleague: {}, log() {} });
  const listed = await call(session, 'tools/list', {});
  const tools = Object.fromEntries(listed.result.tools.map((tool) => [tool.name, tool]));
  const voices = tools.update_profile.inputSchema.properties.voices;
  assert.deepEqual(Object.keys(voices.properties), ['phone', 'meeting']);
  assert.equal(voices.additionalProperties, false);
  assert.match(voices.description, /"" to go back to the default/);
  assert.match(tools.list_voices.description, /defaults\.phone for phone calls, defaults\.meeting for meetings/);
  assert.match(tools.list_voices.description, /A brief's voice wins/);
  assert.match(tools.get_profile.description, /default voices for phone calls and for meetings/);
  assert.match(tools.update_profile.description, /A call keeps its voice until it ends/);
  assert.match(tools.update_profile.description, /once the user agrees/);
  assert.match(tools.start_call.inputSchema.properties.voice.description, /this call only/);
  // Changing a default is a change: clients still ask the user first.
  assert.equal(tools.update_profile.annotations.readOnlyHint, false);
  assert.equal(tools.list_voices.annotations.readOnlyHint, true);
});

test('update_profile passes voices on, and list_voices returns the defaults', async () => {
  const seen = [];
  const defaults = { phone: 'quartz', meeting: 'cinder' };
  const colleague = {
    async listVoices() { return { default: 'quartz', defaults, voices: ['marin', 'quartz', 'cinder'] }; },
    async getProfile() { return { version: 1, voices: defaults }; },
    async updateProfile(update) {
      seen.push(update);
      return { version: 1, voices: { ...defaults, ...update.voices } };
    },
  };
  const session = createMcpSession({ colleague, log() {} });
  const changed = await call(session, 'tools/call', { name: 'update_profile', arguments: { voices: { meeting: 'marin' } } });
  assert.notEqual(changed.result.isError, true);
  assert.deepEqual(seen, [{ voices: { meeting: 'marin' } }]);
  assert.deepEqual(changed.result.structuredContent.voices, { phone: 'quartz', meeting: 'marin' });
  const listed = await call(session, 'tools/call', { name: 'list_voices', arguments: {} });
  assert.deepEqual(listed.result.structuredContent.defaults, defaults);
  const profile = await call(session, 'tools/call', { name: 'get_profile', arguments: {} });
  assert.deepEqual(profile.result.structuredContent.voices, defaults);
});
