#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let failures = 0;

function result(kind, label, detail = '') {
  const prefix = kind === 'pass' ? 'PASS' : kind === 'warn' ? 'WARN' : 'FAIL';
  process.stdout.write(`${prefix}  ${label}${detail ? ` — ${detail}` : ''}\n`);
  if (kind === 'fail') failures += 1;
}

function command(binary, args, options = {}) {
  return spawnSync(binary, args, {
    cwd: root,
    encoding: 'utf8',
    timeout: options.timeout || 10_000,
    env: process.env,
  });
}

function envHasValue(file, key) {
  if (!fs.existsSync(file)) return false;
  return fs.readFileSync(file, 'utf8').split(/\r?\n/).some((line) => {
    const match = line.match(/^\s*([^#=]+)=(.*)$/);
    return match && match[1].trim() === key && match[2].trim().length > 0;
  });
}

const major = Number(process.versions.node.split('.')[0]);
result(major >= 22 ? 'pass' : 'fail', 'Node.js', process.version);

for (const relative of [
  'compose.meeting.yaml',
  'start-runtime-daemon.sh',
  'packages/mcp/src/server.mjs',
  'meeting-runtime/bridge.py',
]) {
  result(fs.existsSync(path.join(root, relative)) ? 'pass' : 'fail', relative);
}

const envFile = path.join(root, '.env');
result(envHasValue(envFile, 'OPENAI_API_KEY') ? 'pass' : 'fail', 'OpenAI API key', '.env contains a non-empty value');
result(envHasValue(envFile, 'TAVILY_API_KEY') ? 'pass' : 'warn', 'Tavily key', 'optional for web search');

const docker = command('docker', ['info'], { timeout: 12_000 });
if (docker.error?.code === 'ETIMEDOUT') result('fail', 'Docker', 'docker info timed out');
else result(docker.status === 0 ? 'pass' : 'fail', 'Docker', docker.status === 0 ? 'daemon is running' : 'daemon is unavailable');

if (docker.status === 0) {
  const compose = command('docker', ['compose', '-f', 'compose.meeting.yaml', 'config', '--quiet']);
  result(compose.status === 0 ? 'pass' : 'fail', 'Meeting Compose configuration');
}

const candidates = [
  process.env.CODEX_BIN,
  '/Applications/ChatGPT.app/Contents/Resources/codex',
  'codex',
].filter(Boolean);
let codex = null;
for (const candidate of candidates) {
  const probe = command(candidate, ['--version']);
  if (probe.status === 0) {
    codex = candidate;
    result('pass', 'Codex CLI', (probe.stdout || probe.stderr).trim());
    break;
  }
}
if (!codex) result('fail', 'Codex CLI', 'not found');
else {
  const login = command(codex, ['login', 'status']);
  result(login.status === 0 ? 'pass' : 'fail', 'Codex login', login.status === 0 ? 'authenticated' : 'run codex login');
  const mcp = command(codex, ['mcp', 'get', 'colleague-ai']);
  result(mcp.status === 0 ? 'pass' : 'warn', 'Codex MCP integration', mcp.status === 0 ? 'registered' : 'run scripts/install-codex-integration.sh');
}

const codexHome = process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
const skillsHome = process.env.CODEX_SKILLS_DIR || path.join(codexHome, 'skills');
const meetingSkill = path.join(skillsHome, 'join-colleague-ai-meeting', 'SKILL.md');
result(
  fs.existsSync(meetingSkill) ? 'pass' : 'fail',
  'Codex exact-continuity skill',
  fs.existsSync(meetingSkill) ? 'installed' : 'run scripts/install-codex-integration.sh',
);

result(fs.existsSync(path.join(root, 'node_modules', 'mammoth')) ? 'pass' : 'fail', 'Node dependencies', 'run npm install when missing');

if (failures) {
  process.stderr.write(`\n${failures} required check${failures === 1 ? '' : 's'} failed.\n`);
  process.exit(1);
}
process.stdout.write('\nColleague AI is ready for a local meeting test.\n');
