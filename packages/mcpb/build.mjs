#!/usr/bin/env node
// Builds dist/smitline.mcpb: refreshes server/tools.json from the real MCP server's answers to
// initialize and tools/list, sets the bundle version from the repository, and zips the bundle.
//   node packages/mcpb/build.mjs [--check]   (--check fails if tools.json is out of date)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import JSZip from 'jszip';
import { createMcpSession } from '../mcp/src/session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '../..');

export async function snapshot() {
  const session = createMcpSession({ createColleague: () => ({}), notify() {}, log() {} });
  const init = await session.dispatch({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'smitline-mcpb-build', version: '1' } } });
  const list = await session.dispatch({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  return `${JSON.stringify({ initialize: init.result, tools: list.result.tools }, null, 2)}\n`;
}

async function main() {
  const toolsFile = path.join(HERE, 'server', 'tools.json');
  const fresh = await snapshot();
  if (process.argv.includes('--check')) {
    if (fs.readFileSync(toolsFile, 'utf8') !== fresh) {
      console.error('packages/mcpb/server/tools.json is out of date: run node packages/mcpb/build.mjs');
      process.exit(1);
    }
    return;
  }
  fs.writeFileSync(toolsFile, fresh);
  const manifest = JSON.parse(fs.readFileSync(path.join(HERE, 'manifest.json'), 'utf8'));
  manifest.version = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8')).version;
  const zip = new JSZip();
  zip.file('manifest.json', `${JSON.stringify(manifest, null, 2)}\n`);
  for (const name of ['icon.png', 'README.md', 'server/index.mjs', 'server/tools.json']) {
    zip.file(name, fs.readFileSync(path.join(HERE, name)));
  }
  zip.file('LICENSE', fs.readFileSync(path.join(ROOT, 'LICENSE')));
  const out = path.join(HERE, 'dist', 'smitline.mcpb');
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE', platform: 'UNIX' }));
  console.log(`${path.relative(ROOT, out)} (version ${manifest.version})`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
