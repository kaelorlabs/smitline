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

export function sseFrame(event) {
  return `id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

export async function startFakeDaemon(options = {}) {
  const token = options.token || 'test-daemon-token';
  const root = options.root;
  if (root) {
    const authPath = path.join(root, '.colleague', 'daemon.auth');
    await fs.mkdir(path.dirname(authPath), { recursive: true, mode: 0o700 });
    await fs.writeFile(authPath, `${token}\n`, { mode: 0o600 });
  }
  const state = {
    token,
    rotatedToken: options.rotatedToken || null,
    created: 0,
    cancels: 0,
    retries: 0,
    contexts: 0,
    meetings: new Map(),
    handoffs: new Map(),
    events: new Map(),
    approvals: new Map(),
    artifacts: new Map(),
    commits: new Map(),
    pushes: new Map(),
    screenShare: new Map(),
    sseClients: [],
    requireAuth: options.requireAuth !== false,
    readyDelayMs: options.readyDelayMs || 0,
    closeStreamAfter: options.closeStreamAfter || 0,
    failCreate: options.failCreate || null,
    unrecoverable: false,
    lastAuthorization: '',
    requests: [],
  };

  function currentToken() {
    return state.token;
  }

  function authorized(request) {
    if (!state.requireAuth) return true;
    const header = request.headers.authorization || '';
    state.lastAuthorization = header;
    const expected = `Bearer ${currentToken()}`;
    return header === expected;
  }

  function meetingEvents(id) {
    if (!state.events.has(id)) state.events.set(id, []);
    return state.events.get(id);
  }

  function pushEvent(id, event) {
    const events = meetingEvents(id);
    events.push(event);
    for (const client of state.sseClients.filter((item) => item.meetingId === id)) {
      if (client.seen.has(event.id)) continue;
      client.seen.add(event.id);
      client.response.write(sseFrame(event));
    }
  }

  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    state.requests.push({ method: request.method, path: url.pathname, lastEventId: request.headers['last-event-id'] || '' });
    if (!authorized(request)) {
      json(response, 401, { error: { code: 'unauthorized', message: 'invalid token' } });
      return;
    }
    try {
      if (request.method === 'GET' && url.pathname === '/v1/providers') {
        json(response, 200, {
          providers: [
            { id: 'codex', installed: true, usable: true, exactSessionResume: true, contextContinuity: true, structuredProgress: true, cancellation: true, handoffAppend: true, workspaceRead: true, workspaceActions: false, supportedModels: [] },
            { id: 'cursor', installed: false, usable: false, exactSessionResume: false, contextContinuity: false, structuredProgress: false, cancellation: true, handoffAppend: false, workspaceRead: true, workspaceActions: false, supportedModels: [], reasonUnavailable: 'missing_binary' },
            { id: 'claude-code', installed: false, usable: false, exactSessionResume: false, contextContinuity: false, structuredProgress: false, cancellation: true, handoffAppend: false, workspaceRead: true, workspaceActions: false, supportedModels: [], reasonUnavailable: 'missing_binary' },
          ],
        });
        return;
      }
      if (request.method === 'POST' && url.pathname === '/v1/meetings') {
        const body = JSON.parse((await readBody(request)) || '{}');
        if (state.failCreate) {
          json(response, state.failCreate.status || 503, { error: state.failCreate });
          return;
        }
        state.created += 1;
        const id = options.meetingId || `mtg-${state.created}`;
        const session = {
          id,
          platform: body.meetingUrl?.includes('teams.') ? 'teams' : body.meetingUrl?.includes('meet.google.com') ? 'meet' : 'zoom',
          meetingUrl: body.meetingUrl,
          agentSession: body.agentSession,
          context: body.context,
          permissions: body.permissions,
          screenShare: body.screenShare,
          state: 'joining',
          startedAt: '2026-09-16T00:00:00Z',
        };
        state.meetings.set(id, session);
        pushEvent(id, {
          version: 1,
          id: 'evt-join',
          meetingId: id,
          timestamp: session.startedAt,
          type: 'meeting.joining',
        });
        if (state.readyDelayMs >= 0) {
          setTimeout(() => {
            session.state = 'live';
            pushEvent(id, {
              version: 1,
              id: 'evt-live',
              meetingId: id,
              timestamp: session.startedAt,
              type: 'meeting.live',
            });
            pushEvent(id, {
              version: 1,
              id: 'evt-transcript',
              meetingId: id,
              timestamp: session.startedAt,
              type: 'transcript.final',
              text: 'secret meeting speech must not be printed',
            });
            pushEvent(id, {
              version: 1,
              id: 'evt-delegation',
              meetingId: id,
              timestamp: session.startedAt,
              type: 'delegation.started',
              taskId: 'task-1',
            });
            if (options.autoHandoff !== false) {
              const handoff = options.handoffFactory?.(id, session) || {
                version: 1,
                meetingId: id,
                startedAt: session.startedAt,
                endedAt: '2026-09-16T00:01:00Z',
                summary: 'done',
                decisions: [],
                requirements: [],
                actionItems: [],
                unresolvedQuestions: [],
                filesDiscussed: [],
                workPerformed: [],
                artifacts: [],
                transcriptPath: `recordings/${id}/transcript.jsonl`,
                recommendedNextAction: 'review',
                handoffId: `hnd-${id}`,
                archivePath: `recordings/${id}`,
                partial: Boolean(options.partial),
              };
              session.state = 'ended';
              state.handoffs.set(id, handoff);
              pushEvent(id, {
                version: 1,
                id: 'evt-ready',
                meetingId: id,
                timestamp: handoff.endedAt,
                type: 'handoff.ready',
                handoff,
              });
            }
          }, state.readyDelayMs);
        }
        json(response, 201, session);
        return;
      }

      const meetingMatch = url.pathname.match(/^\/v1\/meetings\/([^/]+)(?:\/(.*))?$/);
      if (meetingMatch) {
        const meetingId = decodeURIComponent(meetingMatch[1]);
        const rest = meetingMatch[2] || '';
        const meeting = state.meetings.get(meetingId);
        if (!meeting && rest !== 'events') {
          json(response, 404, { error: { code: 'not_found', message: 'meeting not found' } });
          return;
        }
        if (request.method === 'GET' && rest === '') {
          json(response, 200, meeting);
          return;
        }
        if (request.method === 'POST' && rest === 'context') {
          const body = JSON.parse((await readBody(request)) || '{}');
          state.contexts += 1;
          meeting.context = body;
          json(response, 200, meeting);
          return;
        }
        if (request.method === 'POST' && rest === 'cancel') {
          state.cancels += 1;
          meeting.state = 'ended';
          const handoff = state.handoffs.get(meetingId) || {
            version: 1,
            meetingId,
            startedAt: meeting.startedAt,
            endedAt: '2026-09-16T00:02:00Z',
            summary: 'cancelled',
            decisions: [],
            requirements: [],
            actionItems: [],
            unresolvedQuestions: [],
            filesDiscussed: [],
            workPerformed: [],
            artifacts: [],
            transcriptPath: `recordings/${meetingId}/transcript.jsonl`,
            recommendedNextAction: 'stop',
            handoffId: `hnd-${meetingId}`,
            archivePath: `recordings/${meetingId}`,
            partial: Boolean(options.cancelPartial),
            endReason: 'cancelled',
          };
          state.handoffs.set(meetingId, handoff);
          pushEvent(meetingId, {
            version: 1,
            id: `evt-cancel-${state.cancels}`,
            meetingId,
            timestamp: handoff.endedAt,
            type: 'handoff.ready',
            handoff,
          });
          json(response, 200, meeting);
          return;
        }
        if (request.method === 'GET' && rest === 'handoff') {
          if (state.unrecoverable) {
            json(response, 409, {
              error: {
                code: 'finalization_failed',
                message: 'unrecoverable handoff failure',
                archivePath: `recordings/${meetingId}`,
              },
            });
            return;
          }
          const handoff = state.handoffs.get(meetingId);
          if (!handoff) {
            json(response, 404, { error: { code: 'not_ready', message: 'handoff not ready' } });
            return;
          }
          json(response, 200, handoff);
          return;
        }
        if (request.method === 'POST' && rest === 'handoff/retry') {
          state.retries += 1;
          await readBody(request);
          if (options.retryFailsOnce && state.retries === 1) {
            json(response, 409, {
              error: { code: 'handoff_append_failed', message: 'still failing', retryable: true },
            });
            return;
          }
          const meetingRecord = state.meetings.get(meetingId) || { startedAt: '2026-09-16T00:00:00Z' };
          const handoff = {
            version: 1,
            meetingId,
            startedAt: meetingRecord.startedAt,
            endedAt: '2026-09-16T00:03:00Z',
            summary: 'retried',
            decisions: [],
            requirements: [],
            actionItems: [],
            unresolvedQuestions: [],
            filesDiscussed: [],
            workPerformed: [],
            artifacts: [],
            transcriptPath: `recordings/${meetingId}/transcript.jsonl`,
            recommendedNextAction: 'review',
            handoffId: `hnd-${meetingId}`,
            archivePath: `recordings/${meetingId}`,
          };
          state.handoffs.set(meetingId, handoff);
          json(response, 200, handoff);
          return;
        }
        const approvalMatch = rest.match(/^approvals(?:\/([^/]+))?(?:\/(decision))?$/);
        if (approvalMatch) {
          const approvalId = approvalMatch[1];
          const decisionPart = approvalMatch[2];
          if (!state.approvals.has(meetingId)) state.approvals.set(meetingId, []);
          const approvals = state.approvals.get(meetingId);
          if (request.method === 'GET' && !approvalId) {
            json(response, 200, { approvals });
            return;
          }
          if (request.method === 'POST' && !approvalId) {
            const body = JSON.parse((await readBody(request)) || '{}');
            const created = {
              version: 1,
              id: `appr-${approvals.length + 1}`,
              meetingId,
              category: body.category || body.permission || 'commands',
              permission: body.category || body.permission || 'commands',
              summary: body.summary || 'Requested action',
              scope: body.scope || {},
              status: 'pending',
              createdAt: meeting.startedAt,
              expiresAt: '2026-09-16T00:15:00Z',
              delegationId: body.delegationId,
            };
            approvals.push(created);
            pushEvent(meetingId, {
              version: 1,
              id: `evt-approval-${created.id}`,
              meetingId,
              timestamp: created.createdAt,
              type: 'approval.required',
              request: created,
            });
            json(response, 201, created);
            return;
          }
          const found = approvals.find((item) => item.id === approvalId);
          if (!found) {
            json(response, 404, { error: { code: 'not_found', message: 'approval not found' } });
            return;
          }
          if (request.method === 'GET' && !decisionPart) {
            json(response, 200, found);
            return;
          }
          if (request.method === 'POST' && decisionPart === 'decision') {
            const body = JSON.parse((await readBody(request)) || '{}');
            const decision = body.decision;
            if (decision !== 'approved' && decision !== 'denied') {
              json(response, 422, { error: { code: 'invalid_request', message: 'decision is invalid' } });
              return;
            }
            if (found.status === decision) {
              json(response, 200, found);
              return;
            }
            if (found.status !== 'pending') {
              json(response, 409, { error: { code: 'conflict', message: 'approval decision is stale' } });
              return;
            }
            found.status = decision;
            found.decision = decision;
            found.resolvedAt = '2026-09-16T00:05:00Z';
            pushEvent(meetingId, {
              version: 1,
              id: `evt-decision-${found.id}`,
              meetingId,
              timestamp: found.resolvedAt,
              type: decision === 'approved' ? 'approval.approved' : 'approval.denied',
              decision: { approvalId: found.id, decision, decidedAt: found.resolvedAt },
            });
          json(response, 200, found);
            return;
          }
        }
        const commitMatch = rest.match(/^commits(?:\/([^/]+))?$/);
        if (commitMatch) {
          const operationId = commitMatch[1];
          if (!state.commits.has(meetingId)) state.commits.set(meetingId, []);
          const commits = state.commits.get(meetingId);
          if (request.method === 'GET' && !operationId) {
            json(response, 200, { commits });
            return;
          }
          if (request.method === 'POST' && !operationId) {
            const body = JSON.parse((await readBody(request)) || '{}');
            if (body.argv || body.url || body.force || body.command) {
              json(response, 422, { error: { code: 'invalid_request', message: 'raw git is not allowed' } });
              return;
            }
            const existing = commits.find((item) => item.id === body.id);
            if (existing && existing.status === 'completed') {
              json(response, 200, existing);
              return;
            }
            const created = {
              id: body.id || `cmt-${commits.length + 1}`,
              kind: 'commit',
              meetingId,
              status: 'requested',
              request: body,
              approvalId: `appr-commit-${commits.length + 1}`,
            };
            commits.push(created);
            json(response, 201, created);
            return;
          }
          const found = commits.find((item) => item.id === operationId);
          if (!found) {
            json(response, 404, { error: { code: 'not_found', message: 'git operation not found' } });
            return;
          }
          json(response, 200, found);
          return;
        }
        const pushMatch = rest.match(/^pushes(?:\/([^/]+))?$/);
        if (pushMatch) {
          const operationId = pushMatch[1];
          if (!state.pushes.has(meetingId)) state.pushes.set(meetingId, []);
          const pushes = state.pushes.get(meetingId);
          if (request.method === 'GET' && !operationId) {
            json(response, 200, { pushes });
            return;
          }
          if (request.method === 'POST' && !operationId) {
            const body = JSON.parse((await readBody(request)) || '{}');
            if (body.argv || body.url || body.force || body.refspec) {
              json(response, 422, { error: { code: 'invalid_request', message: 'raw git is not allowed' } });
              return;
            }
            const existing = pushes.find((item) => item.id === body.id);
            if (existing && existing.status === 'completed') {
              json(response, 200, existing);
              return;
            }
            const created = {
              id: body.id || `psh-${pushes.length + 1}`,
              kind: 'push',
              meetingId,
              status: 'requested',
              request: body,
              approvalId: `appr-push-${pushes.length + 1}`,
            };
            pushes.push(created);
            json(response, 201, created);
            return;
          }
          const found = pushes.find((item) => item.id === operationId);
          if (!found) {
            json(response, 404, { error: { code: 'not_found', message: 'git operation not found' } });
            return;
          }
          json(response, 200, found);
          return;
        }
        const shareMatch = rest.match(/^screen-share(?:\/(pause|resume|observations))?$/);
        if (shareMatch) {
          if (!state.screenShare.has(meetingId)) {
            state.screenShare.set(meetingId, {
              status: { enabled: Boolean(meeting.screenShare?.enabled), paused: false, available: false, active: false, capturing: false },
              observations: [],
            });
          }
          const share = state.screenShare.get(meetingId);
          const action = shareMatch[1];
          if (request.method === 'GET' && !action) {
            json(response, 200, share);
            return;
          }
          if (request.method === 'GET' && action === 'observations') {
            json(response, 200, { observations: share.observations || [] });
            return;
          }
          if (request.method === 'POST' && (action === 'pause' || action === 'resume')) {
            await readBody(request);
            share.status = { ...share.status, paused: action === 'pause', enabled: true };
            json(response, 200, share);
            return;
          }
        }
        const artifactMatch = rest.match(/^artifacts(?:\/([^/]+))?(?:\/(content))?$/);
        if (artifactMatch && request.method === 'GET') {
          const artifactId = artifactMatch[1];
          const wantContent = artifactMatch[2] === 'content';
          if (!state.artifacts.has(meetingId)) state.artifacts.set(meetingId, []);
          const artifacts = state.artifacts.get(meetingId);
          if (!artifactId) {
            json(response, 200, { artifacts: artifacts.map(({ body, ...meta }) => meta) });
            return;
          }
          if (artifactId.includes('..') || artifactId.includes('/')) {
            json(response, 422, { error: { code: 'invalid_request', message: 'artifactId must not contain a path' } });
            return;
          }
          const found = artifacts.find((item) => item.id === artifactId);
          if (!found) {
            json(response, 404, { error: { code: 'not_found', message: 'artifact not found' } });
            return;
          }
          if (wantContent) {
            const data = Buffer.from(found.body || JSON.stringify(found), 'utf8');
            response.writeHead(200, {
              'Content-Type': found.mediaType || 'application/json',
              'Content-Disposition': `attachment; filename="${found.id}"`,
              'X-Content-Type-Options': 'nosniff',
              'Cache-Control': 'no-store',
            });
            response.end(data);
            return;
          }
          const { body, ...meta } = found;
          json(response, 200, meta);
          return;
        }
        if (request.method === 'GET' && rest === 'events') {
          const lastEventId = request.headers['last-event-id'] || '';
          response.writeHead(200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            Connection: 'keep-alive',
          });
          response.flushHeaders?.();
          response.write(': connected\n\n');
          const events = meetingEvents(meetingId);
          const seen = new Set();
          let start = 0;
          if (lastEventId) {
            const index = events.findIndex((event) => event.id === lastEventId);
            start = index === -1 ? 0 : index + 1;
          }
          for (const event of events.slice(start)) {
            seen.add(event.id);
            response.write(sseFrame(event));
          }
          const client = { meetingId, response, seen };
          state.sseClients.push(client);
          if (state.closeStreamAfter) {
            setTimeout(() => response.end(), state.closeStreamAfter);
          }
          request.on('close', () => {
            state.sseClients = state.sseClients.filter((item) => item !== client);
          });
          return;
        }
      }
      json(response, 404, { error: { code: 'not_found', message: 'no such route' } });
    } catch (error) {
      json(response, 500, { error: { code: 'internal', message: error.message } });
    }
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
      await fs.writeFile(path.join(root, '.colleague', 'daemon.auth'), `${nextToken}\n`, { mode: 0o600 });
    },
    close() {
      return new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
    },
  };
}
