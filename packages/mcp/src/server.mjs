#!/usr/bin/env node
import { createMcpSession } from './session.mjs';
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

  async function onMessage(message) {
    const response = await session.dispatch(message);
    if (response) framer.write(response);
  }

  const reading = readMessages(stdin, (message) => {
    onMessage(message).catch((error) => {
      process.stderr.write(`${redact(error.message)}\n`);
    });
  });

  async function shutdown(signal) {
    process.stderr.write(`colleague-mcp ${signal}: shutting down\n`);
    await session.shutdown();
    process.exit(signal === 'SIGINT' ? 130 : 0);
  }

  if (options.installSignals !== false) {
    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));
  }

  return { session, reading, framer };
}

const invoked = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invoked) {
  startStdioServer();
}
