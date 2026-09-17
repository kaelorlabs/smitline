import {
  Colleague,
  ValidationError,
  StartupError,
  RuntimeError,
  FinalizationError,
  InterruptError,
  redact,
  validateContext,
} from '../../sdk-typescript/src/index.mjs';
import { fileURLToPath } from 'node:url';

export const MCP_PROTOCOL_VERSION = '2025-06-18';
export const MCP_SERVER_VERSION = '1.0.0';
export const TASKS_EXTENSION = 'io.modelcontextprotocol/tasks';
const FORBIDDEN_SESSION_IDS = new Set(['', '--last', 'last', 'latest', '--latest']);
const PROVIDERS = new Set(['codex', 'cursor', 'claude-code', 'generic']);
const DEFAULT_COLLEAGUE_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const DEFAULT_SAFE_PERMISSIONS = Object.freeze({
  workspace: 'read-only',
  commands: 'approval-required',
  edits: 'disabled',
  network: 'approval-required',
  commits: 'disabled',
  pushes: 'disabled',
});

const CONTEXT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['version', 'objective', 'currentTask', 'summary', 'decisions', 'constraints', 'openQuestions', 'importantFiles', 'recentConversation'],
  properties: {
    version: { const: 1 },
    objective: { type: 'string' },
    currentTask: { type: 'string' },
    summary: { type: 'string' },
    decisions: { type: 'array', items: { type: 'string' } },
    constraints: { type: 'array', items: { type: 'string' } },
    openQuestions: { type: 'array', items: { type: 'string' } },
    importantFiles: { type: 'array', items: { type: 'string' } },
    recentConversation: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['role', 'text'],
        properties: {
          role: { enum: ['user', 'assistant'] },
          text: { type: 'string' },
        },
      },
    },
    git: {
      type: 'object',
      properties: {
        branch: { type: 'string' },
        commit: { type: 'string' },
        dirty: { type: 'boolean' },
      },
    },
  },
};

const PERMISSIONS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['workspace', 'commands', 'edits', 'network', 'commits', 'pushes'],
  properties: {
    workspace: { enum: ['none', 'read-only', 'workspace-write'] },
    commands: { enum: ['disabled', 'approval-required', 'allowed'] },
    edits: { enum: ['disabled', 'approval-required', 'allowed'] },
    network: { enum: ['disabled', 'approval-required', 'allowed'] },
    commits: { enum: ['disabled', 'approval-required'] },
    pushes: { enum: ['disabled', 'approval-required'] },
  },
};

export const TOOL_DEFINITIONS = [
  {
    name: 'join_current_meeting',
    description: 'Join a Zoom, Teams, or Google Meet call from the current Codex conversation. The server uses the real CODEX_THREAD_ID supplied by Codex, so callers must not invent or look up a session id. Pass a bounded summary of the current work as context. Returns a meeting handle immediately unless this client advertises MCP Tasks and waitUntilHandoff is true.',
    execution: { taskSupport: 'optional' },
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['url', 'workspace', 'context'],
      properties: {
        url: { type: 'string', description: 'https Zoom, Teams, or Google Meet invitation URL' },
        workspace: { type: 'string', description: 'Absolute path of the current coding workspace' },
        model: { type: 'string' },
        context: CONTEXT_SCHEMA,
        permissions: PERMISSIONS_SCHEMA,
        waitUntilHandoff: { type: 'boolean' },
        cameraEnabled: { type: 'boolean', description: 'When false, join audio-only. Default true.' },
        screenShareEnabled: { type: 'boolean', description: 'When true, observe incoming shared content. Default false.' },
      },
    },
  },
  {
    name: 'start_meeting',
    description: 'Start a Colleague AI Zoom, Teams, or Google Meet meeting through the local daemon. Exact continuity requires the host integration to inject the real originating thread id. Generic MCP clients should set continuity to context. Returns a durable meeting handle immediately; poll get_meeting_handoff unless this client advertised MCP Tasks on the request.',
    execution: { taskSupport: 'optional' },
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['url', 'provider', 'workspace', 'context', 'permissions'],
      properties: {
        url: { type: 'string', description: 'https Zoom, Teams, or Google Meet invitation URL' },
        provider: { enum: [...PROVIDERS] },
        sessionId: { type: 'string', description: 'Originating thread id. Required for exact continuity. Never last/latest.' },
        workspace: { type: 'string', description: 'Absolute workspace path' },
        model: { type: 'string' },
        metadata: { type: 'object', additionalProperties: { type: 'string' } },
        context: CONTEXT_SCHEMA,
        permissions: PERMISSIONS_SCHEMA,
        continuity: { enum: ['exact', 'context'], description: 'Defaults to exact when sessionId is a real thread id.' },
        waitUntilHandoff: { type: 'boolean', description: 'If true and the request advertises MCP Tasks, wait for the durable handoff as a task. Otherwise poll get_meeting_handoff.' },
        cameraEnabled: { type: 'boolean', description: 'When false, join audio-only. Default true.' },
        screenShareEnabled: { type: 'boolean', description: 'When true, capture meeting shared-content at a low rate. Default false. Voice cannot enable this.' },
      },
    },
  },
  {
    name: 'get_meeting_status',
    description: 'Get the current daemon meeting session for a meeting id.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'add_meeting_context',
    description: 'Replace meeting context with a versioned ContextHandoff.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'context'],
      properties: {
        meetingId: { type: 'string' },
        context: CONTEXT_SCHEMA,
      },
    },
  },
  {
    name: 'cancel_meeting',
    description: 'Request clean cancellation. Yields a partial handoff when one is available.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'get_meeting_handoff',
    description: 'Fetch the durable MeetingHandoff when ready. Poll this from clients that do not advertise MCP Tasks.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'retry_meeting_handoff',
    description: 'Retry exact-session handoff append after a retryable finalization failure.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'list_meeting_approvals',
    description: 'List durable approval requests for a meeting id. Requires an explicit meetingId.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'get_meeting_approval',
    description: 'Get one approval request. Requires explicit meetingId and approvalId.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'approvalId'],
      properties: {
        meetingId: { type: 'string' },
        approvalId: { type: 'string' },
      },
    },
  },
  {
    name: 'decide_meeting_approval',
    description: 'Approve or deny a pending approval. Requires explicit meetingId, approvalId, and decision. Voice cannot grant permission.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'approvalId', 'decision'],
      properties: {
        meetingId: { type: 'string' },
        approvalId: { type: 'string' },
        decision: { enum: ['approved', 'denied'] },
      },
    },
  },
  {
    name: 'list_meeting_artifacts',
    description: 'List host-only workspace action artifacts for a meeting id. Requires an explicit meetingId.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'get_meeting_artifact',
    description: 'Get metadata for one workspace artifact. Requires explicit meetingId and artifactId.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'artifactId'],
      properties: {
        meetingId: { type: 'string' },
        artifactId: { type: 'string' },
      },
    },
  },
  {
    name: 'list_meeting_commits',
    description: 'List typed commit operations for a meeting. Requires an explicit meetingId. Never accepts raw git argv.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'get_meeting_commit',
    description: 'Get one typed commit operation. Requires explicit meetingId and operationId.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'operationId'],
      properties: {
        meetingId: { type: 'string' },
        operationId: { type: 'string' },
      },
    },
  },
  {
    name: 'create_meeting_commit',
    description: 'Request an approved commit of an exact file manifest. Requires meetingId, expectedHead, message, and files. Never accepts raw git argv, force, or hooks. Voice cannot grant permission.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'expectedHead', 'message', 'files'],
      properties: {
        meetingId: { type: 'string' },
        id: { type: 'string' },
        expectedHead: { type: 'string' },
        message: { type: 'string' },
        files: {
          type: 'array',
          items: {
            type: 'object',
            additionalProperties: false,
            required: ['path', 'sha256'],
            properties: { path: { type: 'string' }, sha256: { type: 'string' } },
          },
        },
      },
    },
  },
  {
    name: 'list_meeting_pushes',
    description: 'List typed push operations for a meeting. Requires an explicit meetingId. Never accepts raw git argv or remote URLs.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'get_meeting_push',
    description: 'Get one typed push operation. Requires explicit meetingId and operationId.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'operationId'],
      properties: {
        meetingId: { type: 'string' },
        operationId: { type: 'string' },
      },
    },
  },
  {
    name: 'create_meeting_push',
    description: 'Request an approved push of an exact local branch to a configured remote name. Requires meetingId, commitSha, remote, and branch. Never force, delete, tags, or URLs. Voice cannot grant permission.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId', 'commitSha', 'remote', 'branch'],
      properties: {
        meetingId: { type: 'string' },
        id: { type: 'string' },
        commitSha: { type: 'string' },
        remote: { type: 'string' },
        branch: { type: 'string' },
      },
    },
  },
  {
    name: 'get_meeting_screen_share',
    description: 'Get screen-share capture status for a meeting. Requires an explicit meetingId. Does not return image bytes or transcripts.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'pause_meeting_screen_share',
    description: 'Pause shared-content capture. Requires an explicit meetingId. Voice cannot enable capture.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'resume_meeting_screen_share',
    description: 'Resume shared-content capture if it was enabled at start. Requires an explicit meetingId.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'list_meeting_screen_share_observations',
    description: 'List concise shared-content observations. Requires an explicit meetingId. Does not return transcripts or image bytes.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['meetingId'],
      properties: { meetingId: { type: 'string' } },
    },
  },
  {
    name: 'list_coding_providers',
    description: 'List local coding-agent provider capabilities. Does not return credentials, transcripts, or guessed model names.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {},
    },
  },
  {
    name: 'get_runner_status',
    description: 'Show local-loopback runner pairing status. Does not return pairing codes, enrollments, transcripts, or credentials.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {},
    },
  },
  {
    name: 'pair_runner',
    description: 'Start a short-lived single-use runner pairing. The pairing code is revealed once. Local loopback remains the supported mode.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        tenantId: { type: 'string' },
        userId: { type: 'string' },
      },
    },
  },
  {
    name: 'complete_runner_pair',
    description: 'Complete runner pairing with the one-time code. Device enrollment is returned once and is never shown again.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      required: ['pairingId', 'pairingCode'],
      properties: {
        pairingId: { type: 'string' },
        pairingCode: { type: 'string' },
      },
    },
  },
  {
    name: 'unpair_runner',
    description: 'Revoke the paired runner identity. Local loopback remains available.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {},
    },
  },
];

function nowIso() {
  return new Date().toISOString();
}

function describeEvent(event) {
  const type = event?.type || 'event';
  if (String(type).startsWith('transcript.')) return `transcript ${type}`;
  if (String(type).startsWith('delegation.')) {
    const task = event.taskId || event.delegationId || '';
    return `delegation ${type.replace('delegation.', '')}${task ? ` ${task}` : ''}`;
  }
  if (String(type).startsWith('meeting.')) return `state ${type.replace('meeting.', '')}`;
  if (type === 'handoff.ready') return 'handoff ready';
  if (type === 'handoff.append_failed') return 'handoff append failed';
  if (type === 'presence.updated') return `presence ${event.visualState || 'updated'}`;
  if (String(type).startsWith('approval.')) {
    const category = event.request?.category || event.request?.permission || '';
    return `approval ${type.replace('approval.', '')}${category ? ` ${category}` : ''}`;
  }
  if (String(type).startsWith('workspace.action.')) {
    return `workspace ${type.replace('workspace.action.', '')}`;
  }
  if (type === 'artifact.created') return `artifact ${event.artifact?.kind || ''}`.trim();
  if (String(type).startsWith('screen_share.')) return `screen-share ${type.replace('screen_share.', '')}`;
  return String(type);
}

function stripSecrets(value) {
  if (Array.isArray(value)) return value.map(stripSecrets);
  if (!value || typeof value !== 'object') return value;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (/secret|token|password|credential|authorization|apiKey|cookie/i.test(key)) continue;
    out[key] = stripSecrets(item);
  }
  return out;
}

function toolResult(payload, { isError = false } = {}) {
  const safe = stripSecrets(payload);
  return {
    content: [{ type: 'text', text: JSON.stringify(safe) }],
    structuredContent: safe,
    isError,
  };
}

function rpcError(id, code, message, data) {
  return {
    jsonrpc: '2.0',
    id,
    error: {
      code,
      message: redact(message),
      ...(data ? { data: stripSecrets(data) } : {}),
    },
  };
}

function rpcResult(id, result) {
  return { jsonrpc: '2.0', id, result };
}

function requestHasTasks(message) {
  const extensions = message?.params?._meta?.['io.modelcontextprotocol/clientCapabilities']?.extensions
    || message?.params?._meta?.extensions
    || {};
  return Boolean(extensions[TASKS_EXTENSION]);
}

function mapToolError(error) {
  const code = error?.code;
  if (error instanceof ValidationError || code === 'validation') return { code: 'validation', status: 2, error };
  if (error instanceof StartupError || code === 'startup' || code === 'daemon_unavailable' || code === 'supervisor_unavailable') {
    return { code: 'startup', status: 3, error };
  }
  if (error instanceof InterruptError || code === 'interrupt') return { code: 'interrupt', status: 130, error };
  if (error instanceof FinalizationError || code === 'finalization' || code === 'handoff_append_failed' || code === 'finalization_failed') {
    return { code: error?.handoff?.partial ? 'partial' : 'finalization', status: error?.handoff?.partial ? 5 : 6, error };
  }
  if (error instanceof RuntimeError) return { code: 'runtime', status: 4, error };
  return { code: code || 'runtime', status: 4, error };
}

function validateStartArguments(args = {}) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    throw new ValidationError('start_meeting arguments must be an object');
  }
  const continuity = args.continuity || (args.sessionId ? 'exact' : null);
  if (continuity !== 'exact' && continuity !== 'context') {
    throw new ValidationError('start_meeting requires continuity exact (with sessionId) or context');
  }
  if (!PROVIDERS.has(args.provider)) throw new ValidationError('provider is invalid');
  if (typeof args.url !== 'string' || !args.url) throw new ValidationError('url is required');
  if (typeof args.workspace !== 'string' || !args.workspace.startsWith('/')) {
    throw new ValidationError('workspace must be an absolute path');
  }
  if (!args.context) throw new ValidationError('context is required');
  if (!args.permissions) throw new ValidationError('permissions is required');
  validateContext(args.context);
  const sessionId = args.sessionId;
  if (continuity === 'exact') {
    if (typeof sessionId !== 'string' || FORBIDDEN_SESSION_IDS.has(sessionId) || sessionId === 'local-portal') {
      throw new ValidationError('exact continuity requires an explicit originating sessionId; never last/latest/local-portal');
    }
  } else if (sessionId !== undefined && FORBIDDEN_SESSION_IDS.has(sessionId)) {
    throw new ValidationError('sessionId must not be last/latest');
  }
  return continuity;
}

export function createMcpSession(options = {}) {
  const log = options.log || ((line) => {
    process.stderr.write(`${redact(line)}\n`);
  });
  const leaveRunning = Boolean(options.leaveRunningOnShutdown ?? process.env.COLLEAGUE_MCP_LEAVE_RUNNING === '1');
  const createColleague = options.createColleague || (() => new Colleague({
    root: options.root || process.env.COLLEAGUE_ROOT || DEFAULT_COLLEAGUE_ROOT,
    host: '127.0.0.1',
    port: options.port || process.env.COLLEAGUE_DAEMON_PORT,
  }));
  const colleague = options.colleague || createColleague();
  const currentCodexSessionId = options.currentCodexSessionId ?? process.env.CODEX_THREAD_ID;
  const handles = new Map();
  const tasks = new Map();
  const notifications = [];
  let notify = options.notify || ((message) => { notifications.push(message); });
  let initialized = false;
  let clientInfo = {};
  let nextTask = 1;
  let progressSeq = 0;

  function emit(method, params) {
    const message = { jsonrpc: '2.0', method, params };
    notify(message);
    return message;
  }

  function sendProgress(progressToken, message) {
    if (message && /secret meeting speech|transcript text/i.test(message)) return;
    progressSeq += 1;
    if (progressToken) {
      emit('notifications/progress', {
        progressToken,
        progress: progressSeq,
        message,
      });
    }
  }

  function taskSnapshot(task) {
    const snapshot = {
      resultType: task.status === 'working' ? 'complete' : 'complete',
      taskId: task.taskId,
      status: task.status,
      statusMessage: task.statusMessage,
      createdAt: task.createdAt,
      lastUpdatedAt: task.lastUpdatedAt,
      ttlMs: task.ttlMs,
      pollIntervalMs: task.pollIntervalMs,
    };
    if (task.status === 'completed') snapshot.result = task.result;
    if (task.status === 'failed') snapshot.error = task.error;
    return snapshot;
  }

  function attachHandle(handle) {
    handles.set(handle.id, handle);
    handle.on?.('event', (event) => {
      const line = describeEvent(event);
      if (String(event?.type || '').startsWith('transcript.')) {
        sendProgress(handle._progressToken, line);
        return;
      }
      sendProgress(handle._progressToken, line);
      if (handle._taskId) {
        const task = tasks.get(handle._taskId);
        if (task && task.status === 'working') {
          task.statusMessage = line;
          task.lastUpdatedAt = nowIso();
          emit('notifications/tasks', {
            taskId: task.taskId,
            status: task.status,
            statusMessage: line,
          });
        }
      }
    });
    return handle;
  }

  async function resolveHandle(meetingId) {
    if (handles.has(meetingId)) return handles.get(meetingId);
    const transport = colleague._transport;
    if (!transport?.getMeeting) throw new ValidationError(`unknown meeting ${meetingId}`);
    const session = await transport.getMeeting(meetingId);
    const proxy = {
      id: meetingId,
      _session: session,
      status: () => transport.getMeeting(meetingId),
      addContext: (context) => transport.updateContext(meetingId, context),
      cancel: () => transport.cancelMeeting(meetingId),
      retryFinalization: () => transport.retryHandoff(meetingId),
      listApprovals: () => transport.listApprovals(meetingId),
      getApproval: (approvalId) => transport.getApproval(meetingId, approvalId),
      decideApproval: (approvalId, decision) => transport.decideApproval(meetingId, approvalId, decision),
      listArtifacts: () => transport.listArtifacts(meetingId),
      getArtifact: (artifactId) => transport.getArtifact(meetingId, artifactId),
      get finished() {
        return transport.getHandoff(meetingId);
      },
    };
    handles.set(meetingId, proxy);
    return proxy;
  }

  function createWaitTask(handle, progressToken) {
    const taskId = `task-${nextTask}`;
    nextTask += 1;
    const task = {
      taskId,
      status: 'working',
      statusMessage: `waiting for handoff ${handle.id}`,
      createdAt: nowIso(),
      lastUpdatedAt: nowIso(),
      ttlMs: 3_600_000,
      pollIntervalMs: 250,
    };
    tasks.set(taskId, task);
    handle._taskId = taskId;
    handle._progressToken = progressToken;
    Promise.resolve(handle.finished)
      .then((handoff) => {
        task.status = 'completed';
        task.statusMessage = handoff?.partial ? 'partial handoff ready' : 'handoff ready';
        task.lastUpdatedAt = nowIso();
        task.result = toolResult({ meetingId: handle.id, handoff });
        sendProgress(progressToken, task.statusMessage);
        emit('notifications/tasks', { taskId, status: task.status, statusMessage: task.statusMessage });
      })
      .catch((error) => {
        const mapped = mapToolError(error);
        task.status = 'failed';
        task.statusMessage = redact(error.message);
        task.lastUpdatedAt = nowIso();
        task.error = { code: mapped.code, message: redact(error.message), archivePath: error.archivePath };
        emit('notifications/tasks', { taskId, status: 'failed', statusMessage: task.statusMessage });
      });
    return task;
  }

  async function startMeeting(args, message) {
    const continuity = validateStartArguments(args);
    const sessionId = continuity === 'context'
      ? (args.sessionId && !FORBIDDEN_SESSION_IDS.has(args.sessionId) ? args.sessionId : 'local-portal')
      : args.sessionId;
    const metadata = { ...(args.metadata || {}), continuity };
    const handle = await colleague.joinMeeting({
      url: args.url,
      agentSession: {
        provider: args.provider,
        sessionId,
        workspace: args.workspace,
        ...(args.model ? { model: args.model } : {}),
        metadata,
      },
      context: args.context,
      permissions: args.permissions,
      ...(args.cameraEnabled === false ? { camera: { enabled: false } } : {}),
      ...(args.screenShareEnabled === true ? { screenShare: { enabled: true } } : {}),
    });
    attachHandle(handle);
    const progressToken = message?.params?._meta?.progressToken;
    handle._progressToken = progressToken;
    const payload = {
      meetingId: handle.id,
      state: (await handle.status().catch(() => ({ state: 'joining' }))).state || 'joining',
      continuity,
      polling: {
        get_meeting_status: 'get_meeting_status',
        get_meeting_handoff: 'get_meeting_handoff',
      },
    };
    if (args.waitUntilHandoff && requestHasTasks(message)) {
      const task = createWaitTask(handle, progressToken);
      return {
        resultType: 'task',
        taskId: task.taskId,
        status: task.status,
        statusMessage: task.statusMessage,
        createdAt: task.createdAt,
        lastUpdatedAt: task.lastUpdatedAt,
        ttlMs: task.ttlMs,
        pollIntervalMs: task.pollIntervalMs,
      };
    }
    if (args.waitUntilHandoff) {
      payload.note = 'Client did not advertise io.modelcontextprotocol/tasks on this request; poll get_meeting_handoff.';
    }
    return toolResult(payload);
  }

  async function joinCurrentMeeting(args, message) {
    if (typeof currentCodexSessionId !== 'string'
        || FORBIDDEN_SESSION_IDS.has(currentCodexSessionId)
        || currentCodexSessionId === 'local-portal') {
      throw new ValidationError(
        'Codex did not provide CODEX_THREAD_ID to this MCP server; restart Codex after installing the integration or use start_meeting with an explicit real sessionId',
      );
    }
    return startMeeting({
      ...args,
      provider: 'codex',
      sessionId: currentCodexSessionId,
      continuity: 'exact',
      permissions: args.permissions || DEFAULT_SAFE_PERMISSIONS,
      metadata: { source: 'codex-current-session' },
    }, message);
  }

  async function callTool(name, args, message) {
    if (name === 'join_current_meeting') return joinCurrentMeeting(args, message);
    if (name === 'start_meeting') return startMeeting(args, message);
    if (name === 'get_meeting_status') {
      const handle = await resolveHandle(args.meetingId);
      return toolResult(await handle.status());
    }
    if (name === 'add_meeting_context') {
      const handle = await resolveHandle(args.meetingId);
      return toolResult(await handle.addContext(args.context));
    }
    if (name === 'cancel_meeting') {
      const handle = await resolveHandle(args.meetingId);
      const session = await handle.cancel();
      let handoff = null;
      try {
        handoff = await Promise.race([
          Promise.resolve(handle.finished),
          new Promise((resolve) => setTimeout(() => resolve(null), 50)),
        ]);
      } catch {
        handoff = null;
      }
      if (handle._taskId && tasks.has(handle._taskId)) {
        const task = tasks.get(handle._taskId);
        task.status = 'cancelled';
        task.statusMessage = 'cancelled';
        task.lastUpdatedAt = nowIso();
        if (handoff) task.result = toolResult({ meetingId: handle.id, handoff, cancelled: true });
      }
      return toolResult({ meetingId: handle.id, state: session.state, handoff, cancelled: true });
    }
    if (name === 'get_meeting_handoff') {
      const handle = await resolveHandle(args.meetingId);
      try {
        const handoff = await (colleague._transport?.getHandoff
          ? colleague._transport.getHandoff(args.meetingId)
          : handle.finished);
        return toolResult({ meetingId: args.meetingId, handoff });
      } catch (error) {
        const mapped = mapToolError(error);
        const payload = {
          meetingId: args.meetingId,
          ready: false,
          code: mapped.code,
          message: redact(error.message),
          archivePath: error.archivePath,
        };
        return toolResult(payload, { isError: mapped.code === 'finalization' && !error.handoff?.partial });
      }
    }
    if (name === 'retry_meeting_handoff') {
      const handle = await resolveHandle(args.meetingId);
      return toolResult({ meetingId: args.meetingId, handoff: await handle.retryFinalization() });
    }
    if (name === 'list_meeting_approvals') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.listApprovals
        ? await handle.listApprovals()
        : await transport.listApprovals(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'get_meeting_approval') {
      if (!args?.meetingId || !args?.approvalId) {
        throw new ValidationError('meetingId and approvalId are required');
      }
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const approval = handle.getApproval
        ? await handle.getApproval(args.approvalId)
        : await transport.getApproval(args.meetingId, args.approvalId);
      return toolResult({ meetingId: args.meetingId, approval });
    }
    if (name === 'decide_meeting_approval') {
      if (!args?.meetingId || !args?.approvalId) {
        throw new ValidationError('meetingId and approvalId are required');
      }
      if (args.decision !== 'approved' && args.decision !== 'denied') {
        throw new ValidationError('decision must be approved or denied');
      }
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const approval = handle.decideApproval
        ? await handle.decideApproval(args.approvalId, args.decision)
        : await transport.decideApproval(args.meetingId, args.approvalId, { decision: args.decision });
      return toolResult({ meetingId: args.meetingId, approval });
    }
    if (name === 'list_meeting_artifacts') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.listArtifacts
        ? await handle.listArtifacts()
        : await transport.listArtifacts(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'get_meeting_artifact') {
      if (!args?.meetingId || !args?.artifactId) {
        throw new ValidationError('meetingId and artifactId are required');
      }
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const artifact = handle.getArtifact
        ? await handle.getArtifact(args.artifactId)
        : await transport.getArtifact(args.meetingId, args.artifactId);
      return toolResult({ meetingId: args.meetingId, artifact });
    }
    if (name === 'list_meeting_commits') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.listCommits
        ? await handle.listCommits()
        : await transport.listCommits(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'get_meeting_commit') {
      if (!args?.meetingId || !args?.operationId) {
        throw new ValidationError('meetingId and operationId are required');
      }
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const commit = handle.getCommit
        ? await handle.getCommit(args.operationId)
        : await transport.getCommit(args.meetingId, args.operationId);
      return toolResult({ meetingId: args.meetingId, commit });
    }
    if (name === 'create_meeting_commit') {
      if (!args?.meetingId || !args?.expectedHead || !args?.message || !args?.files) {
        throw new ValidationError('meetingId, expectedHead, message, and files are required');
      }
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const commit = handle.createCommit
        ? await handle.createCommit(args)
        : await transport.createCommit(args.meetingId, args);
      return toolResult({ meetingId: args.meetingId, commit });
    }
    if (name === 'list_meeting_pushes') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.listPushes
        ? await handle.listPushes()
        : await transport.listPushes(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'get_meeting_push') {
      if (!args?.meetingId || !args?.operationId) {
        throw new ValidationError('meetingId and operationId are required');
      }
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const push = handle.getPush
        ? await handle.getPush(args.operationId)
        : await transport.getPush(args.meetingId, args.operationId);
      return toolResult({ meetingId: args.meetingId, push });
    }
    if (name === 'create_meeting_push') {
      if (!args?.meetingId || !args?.commitSha || !args?.remote || !args?.branch) {
        throw new ValidationError('meetingId, commitSha, remote, and branch are required');
      }
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const push = handle.createPush
        ? await handle.createPush(args)
        : await transport.createPush(args.meetingId, args);
      return toolResult({ meetingId: args.meetingId, push });
    }
    if (name === 'get_meeting_screen_share') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.getScreenShare
        ? await handle.getScreenShare()
        : await transport.getScreenShare(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'pause_meeting_screen_share') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.pauseScreenShare
        ? await handle.pauseScreenShare()
        : await transport.pauseScreenShare(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'resume_meeting_screen_share') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.resumeScreenShare
        ? await handle.resumeScreenShare()
        : await transport.resumeScreenShare(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'list_meeting_screen_share_observations') {
      if (!args?.meetingId) throw new ValidationError('meetingId is required');
      const handle = await resolveHandle(args.meetingId);
      const transport = colleague._transport;
      const payload = handle.listScreenShareObservations
        ? await handle.listScreenShareObservations()
        : await transport.listScreenShareObservations(args.meetingId);
      return toolResult({ meetingId: args.meetingId, ...payload });
    }
    if (name === 'list_coding_providers') {
      const transport = colleague._transport;
      const payload = colleague.listProviders
        ? await colleague.listProviders()
        : await transport.listProviders();
      return toolResult(payload);
    }
    if (name === 'get_runner_status') {
      const transport = colleague._transport || {};
      const payload = colleague.runnerStatus
        ? await colleague.runnerStatus()
        : await transport.runnerStatus();
      return toolResult(payload);
    }
    if (name === 'pair_runner') {
      const transport = colleague._transport || {};
      const payload = colleague.pairRunner
        ? await colleague.pairRunner(args || {})
        : await transport.pairRunner(args || {});
      return toolResult(payload);
    }
    if (name === 'complete_runner_pair') {
      if (!args?.pairingId || !args?.pairingCode) {
        throw new ValidationError('pairingId and pairingCode are required');
      }
      const transport = colleague._transport || {};
      const payload = colleague.completeRunnerPair
        ? await colleague.completeRunnerPair(args)
        : await transport.completeRunnerPair(args);
      return toolResult(payload);
    }
    if (name === 'unpair_runner') {
      const transport = colleague._transport || {};
      const payload = colleague.unpairRunner
        ? await colleague.unpairRunner()
        : await transport.unpairRunner();
      return toolResult(payload);
    }
    throw new ValidationError(`unknown tool: ${name}`);
  }

  async function dispatch(message) {
    if (!message || message.jsonrpc !== '2.0') {
      return rpcError(message?.id ?? null, -32600, 'invalid JSON-RPC message');
    }
    const { id, method, params } = message;
    if (method && method.startsWith('notifications/')) {
      if (method === 'notifications/initialized') initialized = true;
      return null;
    }
    if (method === 'initialize') {
      clientInfo = params?.clientInfo || {};
      initialized = true;
      return rpcResult(id, {
        protocolVersion: params?.protocolVersion || MCP_PROTOCOL_VERSION,
        capabilities: {
          tools: { listChanged: false },
          extensions: { [TASKS_EXTENSION]: {} },
        },
        serverInfo: { name: 'colleague-ai', version: MCP_SERVER_VERSION },
        instructions: 'Colleague AI MCP adapter talks only to the loopback daemon. In Codex, use join_current_meeting so the server uses the real CODEX_THREAD_ID supplied by the host. Other integrations must inject the real originating sessionId into start_meeting for exact continuity. Generic MCP clients must set continuity=context. Do not pass last/latest. Poll get_meeting_handoff unless this client advertises io.modelcontextprotocol/tasks on waitUntilHandoff calls.',
      });
    }
    if (method === 'ping') return rpcResult(id, {});
    if (method === 'tools/list') {
      return rpcResult(id, { tools: TOOL_DEFINITIONS });
    }
    if (method === 'tools/call') {
      try {
        const result = await callTool(params?.name, params?.arguments || {}, message);
        return rpcResult(id, result);
      } catch (error) {
        const mapped = mapToolError(error);
        return rpcResult(id, toolResult({
          code: mapped.code,
          message: redact(error.message),
          archivePath: error.archivePath,
        }, { isError: true }));
      }
    }
    if (method === 'tasks/get') {
      if (!requestHasTasks(message) && !options.allowTasksWithoutMeta) {
        return rpcError(id, -32021, 'Missing required client capability', {
          requiredCapabilities: { extensions: { [TASKS_EXTENSION]: {} } },
        });
      }
      const task = tasks.get(params?.taskId);
      if (!task) return rpcError(id, -32001, 'task not found');
      return rpcResult(id, taskSnapshot(task));
    }
    if (method === 'tasks/cancel') {
      const task = tasks.get(params?.taskId);
      if (!task) return rpcError(id, -32001, 'task not found');
      const handle = [...handles.values()].find((item) => item._taskId === task.taskId);
      if (handle) {
        try { await handle.cancel(); } catch { /* still mark cancelled */ }
      }
      task.status = 'cancelled';
      task.statusMessage = 'cancelled';
      task.lastUpdatedAt = nowIso();
      return rpcResult(id, { ...taskSnapshot(task), resultType: 'complete' });
    }
    if (method === 'tasks/update') {
      return rpcError(id, -32601, 'tasks/update is not required for Colleague AI wait-until-handoff');
    }
    return rpcError(id, -32601, `method not found: ${method}`);
  }

  async function shutdown() {
    if (leaveRunning) return { leftRunning: [...handles.keys()] };
    const ids = [];
    for (const handle of handles.values()) {
      ids.push(handle.id);
      try { await handle.cancel(); } catch { /* ignore */ }
    }
    return { cancelled: ids };
  }

  return {
    dispatch,
    shutdown,
    notifications,
    setNotify(fn) { notify = fn; },
    handles,
    tasks,
    get initialized() { return initialized; },
    get clientInfo() { return clientInfo; },
    log,
  };
}
