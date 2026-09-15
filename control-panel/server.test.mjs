import assert from 'node:assert/strict';
import { once } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { createServer } from './server.mjs';

async function withServer(run) {
  const server = createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const { port } = server.address();
    await run(`http://127.0.0.1:${port}`);
  } finally {
    server.close();
    await once(server, 'close');
  }
}

test('serves the console with local security headers', async () => {
  await withServer(async base => {
    const response = await fetch(`${base}/`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-security-policy'), /default-src 'self'/);
    assert.match(await response.text(), /Meeting details/);
  });
});

test('bootstrap excludes API credentials and mutations require a session token', async () => {
  await withServer(async base => {
    const bootstrap = await fetch(`${base}/api/bootstrap`);
    const body = await bootstrap.json();
    assert.equal(bootstrap.status, 200);
    assert.equal(typeof body.token, 'string');
    assert.equal(Array.isArray(body.context.sources), true);
    assert.equal('passcode' in body.settings, false);
    assert.equal('OPENAI_API_KEY' in body.settings, false);

    const mutation = await fetch(`${base}/api/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    assert.equal(mutation.status, 403);
  });
});

test('adds private context without returning its extracted text in bootstrap', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-server-context-'));
  const contextIndex = path.join(directory, 'index.json');
  const server = createServer({ contextIndex });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const bootstrap = await (await fetch(`${base}/api/bootstrap`)).json();
    const response = await fetch(`${base}/api/context/add`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Origin': 'http://127.0.0.1:8095',
        'X-Colleague-Token': bootstrap.token,
      },
      body: JSON.stringify({ text: 'Private launch target is October 4.', files: [] }),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(result.sources.length, 1);
    assert.equal('text' in result.sources[0], false);
    const refreshed = await (await fetch(`${base}/api/bootstrap`)).json();
    assert.equal(refreshed.context.sources[0].name, 'Pasted meeting context');
    assert.equal(JSON.stringify(refreshed).includes('Private launch target'), false);
  } finally {
    server.close();
    await once(server, 'close');
  }
});

test('meeting controls reject unauthenticated requests', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const endpoint of ['/api/platforms/teams/connect', '/api/platforms/teams/disconnect']) {
      const result = await fetch(base + endpoint, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({allowed:true}) });
      assert.equal(result.status, 403);
    }
  } finally { await new Promise(resolve => server.close(resolve)); }
});
