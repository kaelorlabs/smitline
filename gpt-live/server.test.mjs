import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createServer } from './server.mjs';

test('gateway validates requests and keeps credentials server-side', async () => {
  let calls = 0;
  const server = createServer('test-secret', async (url, options) => {
    calls++;
    assert.equal(url, 'https://api.openai.com/v1/live/sessions');
    assert.equal(options.headers.Authorization, 'Bearer test-secret');
    const body = JSON.parse(options.body);
    assert.equal(body.session.model, 'gpt-live-1');
    assert.equal(body.transport.sdp, 'v=0\r\n');
    return Response.json({ session: { id: 'live_test' }, transport: { sdp: 'answer' } });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const host = `127.0.0.1:${process.env.PORT || 8093}`;
  function request(path, body, overrides = {}) {
    return new Promise((resolve, reject) => {
      const req = http.request({ hostname: '127.0.0.1', port: server.address().port, path,
        method: body === undefined ? 'GET' : 'POST',
        headers: { Host: host, Origin: `http://${host}`, 'Content-Type': 'application/json', ...overrides },
      }, res => {
        let text = '';
        res.on('data', chunk => { text += chunk; });
        res.on('end', () => resolve({ status: res.statusCode, text }));
      });
      req.on('error', reject);
      req.end(body);
    });
  }
  try {
    assert.equal((await request('/api/session', '{}', { Origin: 'http://other.test' })).status, 403);
    assert.equal((await request('/api/session', '{')).status, 400);
    assert.equal((await request('/api/session', '{"sdp":"bad"}')).status, 400);
    assert.equal(calls, 0);
    const response = await request('/api/session', JSON.stringify({ sdp: 'v=0\r\n', session: { model: 'wrong' } }));
    assert.equal(response.status, 201);
    assert.equal(JSON.parse(response.text).session.id, 'live_test');
    assert.equal(response.text.includes('test-secret'), false);
    assert.equal(calls, 1);
    assert.equal((await request('/.env')).status, 404);
    assert.equal((await request('/health')).text.includes('test-secret'), false);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});
