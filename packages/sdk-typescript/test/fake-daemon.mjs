import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';

function json(response, status, payload) {
  response.writeHead(status, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify(payload));
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    request.on('error', reject);
  });
}

/** A loopback daemon serving the calls API: phone calls and meetings from a brief. */
export async function startFakeDaemon(options = {}) {
  const token = options.token || 'test-daemon-token';
  const root = options.root;
  if (root) {
    const authPath = path.join(root, '.smitline', 'daemon.auth');
    await fs.mkdir(path.dirname(authPath), { recursive: true, mode: 0o700 });
    await fs.writeFile(authPath, `${token}\n`, { mode: 0o600 });
  }
  const state = {
    token,
    created: 0,
    calls: new Map(),
    instructions: [],
    profile: { version: 1 },
    requireAuth: options.requireAuth !== false,
    // Wait requests answered before a call completes; 0 completes on the first wait.
    pollsBeforeDone: options.pollsBeforeDone || 0,
    failCreate: options.failCreate || null,
    lastAuthorization: '',
    requests: [],
  };

  function authorized(request) {
    if (!state.requireAuth) return true;
    const header = request.headers.authorization || '';
    state.lastAuthorization = header;
    return header === `Bearer ${state.token}`;
  }

  function checkBrief(brief) {
    const missing = [];
    if (!brief.objective) missing.push({ field: 'objective', question: 'What should the call achieve?' });
    if (!['phone', 'meeting'].includes(brief.channel)) missing.push({ field: 'channel', question: 'Phone call or meeting?' });
    return missing;
  }

  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    const body = request.method === 'GET' ? null : JSON.parse((await readBody(request)) || 'null');
    state.requests.push({ method: request.method, path: url.pathname, search: url.search, body });
    if (!authorized(request)) {
      json(response, 401, { error: { code: 'unauthorized', message: 'invalid token' } });
      return;
    }
    if (request.method === 'GET' && url.pathname === '/v1/voices') {
      json(response, 200, { default: 'marin', voices: ['marin', 'cinder'] });
      return;
    }
    if (url.pathname === '/v1/profile') {
      if (request.method === 'PATCH') state.profile = { ...state.profile, ...body };
      json(response, 200, state.profile);
      return;
    }
    if (request.method === 'POST' && url.pathname === '/v1/calls/check') {
      const missing = checkBrief(body || {});
      json(response, 200, { ok: missing.length === 0, brief: body, problems: missing.map((item) => item.field) });
      return;
    }
    if (request.method === 'POST' && url.pathname === '/v1/calls') {
      if (state.failCreate) {
        json(response, state.failCreate.status || 503, { error: state.failCreate });
        return;
      }
      const missing = checkBrief(body || {});
      if (missing.length) {
        json(response, 422, { error: { code: 'brief_incomplete', message: `brief is missing ${missing[0].field}`, missing } });
        return;
      }
      state.created += 1;
      const id = `call-${String(state.created).padStart(16, '0')}`;
      const call = {
        id,
        channel: body.channel,
        status: 'queued',
        brief: body,
        createdAt: '2026-09-16T00:00:00Z',
        result: null,
        polls: 0,
      };
      state.calls.set(id, call);
      json(response, 201, call);
      return;
    }
    if (request.method === 'GET' && url.pathname === '/v1/calls') {
      const limit = Number(url.searchParams.get('limit') || 20);
      json(response, 200, { calls: [...state.calls.values()].reverse().slice(0, limit) });
      return;
    }
    const match = url.pathname.match(/^\/v1\/calls\/([^/]+)(?:\/(wait|end|transfer|instructions))?$/);
    const call = match && state.calls.get(decodeURIComponent(match[1]));
    if (!call) {
      json(response, 404, { error: { code: 'not_found', message: match ? 'call not found' : 'no such route' } });
      return;
    }
    const action = match[2] || '';
    if (request.method === 'GET' && action === '') {
      json(response, 200, call);
      return;
    }
    if (request.method === 'GET' && action === 'wait') {
      call.polls += 1;
      if (call.status !== 'canceled' && call.polls > state.pollsBeforeDone) {
        call.status = 'completed';
        call.endReason = call.channel === 'meeting' ? 'meeting_ended' : 'hangup';
        call.result = { outcome: 'achieved', summary: 'Done.', details: [], decisions: [], actionItems: [], openQuestions: [], transcript: [] };
      } else if (call.status === 'queued') {
        call.status = call.channel === 'meeting' ? 'waiting' : 'ringing';
      }
      json(response, 200, call);
      return;
    }
    if (request.method === 'POST' && action === 'end') {
      call.status = 'canceled';
      json(response, 200, call);
      return;
    }
    if (request.method === 'POST' && action === 'transfer') {
      json(response, 200, { transferred: true });
      return;
    }
    if (request.method === 'POST' && action === 'instructions') {
      state.instructions.push({ callId: call.id, ...body });
      json(response, 200, { delivered: true });
      return;
    }
    json(response, 404, { error: { code: 'not_found', message: 'no such route' } });
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    port,
    token,
    state,
    url: `http://127.0.0.1:${port}`,
    rotateToken(next = 'rotated-daemon-token') {
      state.token = next;
      return next;
    },
    async writeAuth(nextToken) {
      if (!root) return;
      await fs.writeFile(path.join(root, '.smitline', 'daemon.auth'), `${nextToken}\n`, { mode: 0o600 });
    },
    close() {
      return new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
    },
  };
}
