#!/usr/bin/env node
import '../../cli/src/adopt-old-settings.mjs';
import { createMcpSession } from './session.mjs';
import { checkModernBody, isModern, listenAcknowledgment } from './modern.mjs';
import { redact } from '../../sdk-typescript/src/index.mjs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

function encodeNdjson(message) {
  return `${JSON.stringify(message)}\n`;
}

function encodeContentLength(message) {
  const json = JSON.stringify(message);
  const body = Buffer.from(json, 'utf8');
  return Buffer.concat([Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, 'utf8'), body]);
}

export function createStdioFramer({ stdout, framing = 'ndjson' }) {
  const write = (message) => {
    if (framing === 'content-length') stdout.write(encodeContentLength(message));
    else stdout.write(encodeNdjson(message));
  };
  return { write, encodeNdjson, encodeContentLength };
}

export async function readMessages(stream, onMessage) {
  let buffer = Buffer.alloc(0);
  let mode = null;
  for await (const chunk of stream) {
    buffer = Buffer.concat([buffer, Buffer.from(chunk)]);
    while (buffer.length) {
      if (mode === 'content-length' || buffer.slice(0, 15).toString('utf8').startsWith('Content-Length:')) {
        mode = 'content-length';
        const headerEnd = buffer.indexOf('\r\n\r\n');
        if (headerEnd === -1) break;
        const header = buffer.slice(0, headerEnd).toString('utf8');
        const match = header.match(/Content-Length:\s*(\d+)/i);
        if (!match) {
          buffer = buffer.slice(headerEnd + 4);
          continue;
        }
        const length = Number(match[1]);
        const start = headerEnd + 4;
        if (buffer.length < start + length) break;
        const json = buffer.slice(start, start + length).toString('utf8');
        buffer = buffer.slice(start + length);
        onMessage(JSON.parse(json));
        continue;
      }
      const nl = buffer.indexOf('\n');
      if (nl === -1) break;
      const line = buffer.slice(0, nl).toString('utf8').replace(/\r$/, '');
      buffer = buffer.slice(nl + 1);
      if (!line.trim()) continue;
      onMessage(JSON.parse(line));
    }
  }
}

export function startStdioServer(options = {}) {
  const stdout = options.stdout || process.stdout;
  const stdin = options.stdin || process.stdin;
  const framing = options.framing || 'ndjson';
  const framer = createStdioFramer({ stdout, framing });
  const session = createMcpSession({
    ...options,
    notify(message) {
      framer.write(message);
    },
    log(line) {
      process.stderr.write(`${redact(line)}\n`);
    },
  });

  // Both eras share the process (modern.mjs): a legacy initialize starts the legacy session, and a
  // message naming its version in _meta is answered on its own, with errors in-band (stdio has no
  // HTTP status).
  async function answer(message) {
    if (!isModern(message)) {
      const reply = await session.dispatch(message);
      return reply ? [reply] : [];
    }
    if (message.id === undefined) return [];
    const refused = checkModernBody(message);
    if (refused) return [refused];
    const reply = await session.dispatchModern(message);
    if (message.method === 'subscriptions/listen' && reply.result) return [listenAcknowledgment(message.id), reply];
    return [reply];
  }

  // Requests being answered, and the ones the client cancelled (notifications/cancelled): nothing
  // more is sent for a cancelled request.
  const inFlight = new Set();
  const cancelled = new Set();

  async function onMessage(message) {
    if (message?.method === 'notifications/cancelled') {
      const requestId = message.params?.requestId;
      if (inFlight.has(requestId)) cancelled.add(requestId);
      return;
    }
    const id = message?.id;
    const tracked = (typeof id === 'string' || typeof id === 'number') && !inFlight.has(id);
    if (tracked) inFlight.add(id);
    try {
      const replies = await answer(message);
      if (tracked && cancelled.has(id)) return;
      for (const reply of replies) framer.write(reply);
    } finally {
      if (tracked) {
        inFlight.delete(id);
        cancelled.delete(id);
      }
    }
  }

  const pending = new Set();
  const reading = readMessages(stdin, (message) => {
    const handled = onMessage(message).catch((error) => {
      process.stderr.write(`${redact(error.message)}\n`);
    });
    pending.add(handled);
    handled.finally(() => pending.delete(handled));
  });
  // Resolves once stdin has ended and every request read so far has been answered.
  const drained = reading.then(async () => {
    while (pending.size) await Promise.allSettled([...pending]);
  });

  async function shutdown(signal) {
    process.stderr.write(`smitline-mcp ${signal}: shutting down\n`);
    await session.shutdown();
    process.exit(signal === 'SIGINT' ? 130 : 0);
  }

  if (options.installSignals !== false) {
    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));
  }

  return { session, reading, drained, framer };
}

const invoked = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) {
  startStdioServer();
}
