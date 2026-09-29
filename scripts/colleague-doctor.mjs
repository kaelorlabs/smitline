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

function envValue(file, key) {
  if (!fs.existsSync(file)) return '';
  let value = '';
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const match = line.match(/^\s*([^#=]+)=(.*)$/);
    if (match && match[1].trim() === key) value = match[2].trim().replace(/^(['"])(.*)\1$/, '$2');
  }
  return value;
}

// .env.example ships values such as replace_with_your_project_api_key.
function isPlaceholder(value) {
  return /replace_with|your_|^<.*>$/i.test(value);
}

function envHasValue(file, key) {
  const value = envValue(file, key);
  return value.length > 0 && !isPlaceholder(value);
}

// WSL2 kernels report "microsoft-standard(-WSL2)" and set WSL_INTEROP; WSL1 reports "Microsoft".
function wslVersion() {
  if (process.platform !== 'linux') return 0;
  let version = '';
  try {
    version = fs.readFileSync('/proc/version', 'utf8');
  } catch {
    return 0;
  }
  if (!/microsoft/i.test(version) && !process.env.WSL_INTEROP) return 0;
  return /WSL2|microsoft-standard/i.test(version) || process.env.WSL_INTEROP ? 2 : 1;
}

function firstLine(text) {
  return String(text || '').split(/\r?\n/).map((line) => line.trim()).find(Boolean) || '';
}

const major = Number(process.versions.node.split('.')[0]);
result(major >= 22 ? 'pass' : 'fail', 'Node.js', process.version);

const wsl = wslVersion();
if (process.platform === 'win32') {
  result('fail', 'Operating system', 'Windows is supported through WSL2: clone and run Colleague AI inside Ubuntu on WSL2');
} else if (wsl) {
  if (wsl === 2) result('pass', 'Operating system', 'Linux on WSL2');
  else result('fail', 'Operating system', 'Linux on WSL1; Docker needs WSL2: run wsl --set-version <distro> 2 in Windows');
  if (root.startsWith('/mnt/')) {
    result('warn', 'Checkout location', 'clone inside the Linux home directory; Docker may not see /mnt drives and file access is slow');
  }
} else {
  result(['linux', 'darwin'].includes(process.platform) ? 'pass' : 'fail', 'Operating system', process.platform);
}

const launcher = path.join(root, 'start-runtime-daemon.sh');
if (fs.existsSync(launcher) && fs.readFileSync(launcher, 'utf8').includes('\r\n')) {
  result('fail', 'Line endings', 'shell scripts have Windows line endings; clone again inside WSL, or stash your changes and run git rm -r -q --cached . && git reset --hard');
}

for (const relative of [
  'compose.meeting.yaml',
  'start-runtime-daemon.sh',
  'packages/mcp/src/server.mjs',
  'meeting-runtime/bridge.py',
]) {
  result(fs.existsSync(path.join(root, relative)) ? 'pass' : 'fail', relative);
}

const envFile = path.join(root, '.env');
const openaiKey = envValue(envFile, 'OPENAI_API_KEY');
if (envHasValue(envFile, 'OPENAI_API_KEY')) result('pass', 'OpenAI API key', '.env contains a value');
else result('fail', 'OpenAI API key', openaiKey ? '.env still has the example placeholder' : 'missing from .env');
result(envHasValue(envFile, 'TAVILY_API_KEY') ? 'pass' : 'warn', 'Tavily key', 'optional for web search');

const docker = command('docker', ['info'], { timeout: 12_000 });
if (docker.error?.code === 'ETIMEDOUT') result('fail', 'Docker', 'docker info timed out');
else result(docker.status === 0 ? 'pass' : 'fail', 'Docker', docker.status === 0 ? 'daemon is running' : 'daemon is unavailable');

// start-runtime-daemon.sh runs the daemon on python3 with venv, or in Docker without it.
const python = command('python3', ['-c', 'import venv, ensurepip']);
if (python.status === 0) result('pass', 'Daemon runtime', 'Python on this computer');
else if (docker.status === 0) result('pass', 'Daemon runtime', 'Docker; python3 with venv is only needed to hand meeting work to a coding agent');
else result('fail', 'Daemon runtime', 'start Docker, or install python3 with venv (on Debian/Ubuntu: sudo apt install python3-venv)');

if (docker.status === 0) {
  const compose = command('docker', ['compose', '-f', 'compose.meeting.yaml', 'config', '--quiet']);
  result(compose.status === 0 ? 'pass' : 'fail', 'Meeting Compose configuration',
    compose.status === 0 ? '' : firstLine(compose.stderr) || firstLine(compose.error?.message));
}

// Codex, its skill, and its launcher are only needed for Codex exact continuity.
const codexHome = process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
const skillsHome = process.env.CODEX_SKILLS_DIR || path.join(codexHome, 'skills');
const meetingSkill = path.join(skillsHome, 'join-colleague-ai-meeting', 'SKILL.md');
const colleagueCli = path.join(process.env.CODEX_BIN_DIR || path.join(codexHome, 'bin'), 'colleague');
const codexContinuity = process.argv.includes('--codex') || fs.existsSync(meetingSkill) || fs.existsSync(colleagueCli);
const codexKind = codexContinuity ? 'fail' : 'warn';
const codexNote = codexContinuity ? '' : ' (optional; needed only for Codex exact continuity)';

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
if (!codex) result(codexKind, 'Codex CLI', `not found${codexNote}`);
else {
  const login = command(codex, ['login', 'status']);
  result(login.status === 0 ? 'pass' : codexKind, 'Codex login', login.status === 0 ? 'authenticated' : `run codex login${codexNote}`);
  const mcp = command(codex, ['mcp', 'get', 'colleague-ai']);
  result(mcp.status === 0 ? 'pass' : 'warn', 'Codex MCP integration', mcp.status === 0 ? 'registered' : 'run scripts/install-codex-integration.sh');
}

result(
  fs.existsSync(meetingSkill) ? 'pass' : codexKind,
  'Codex exact-continuity skill',
  fs.existsSync(meetingSkill) ? 'installed' : `run scripts/install-codex-integration.sh${codexNote}`,
);
result(
  fs.existsSync(colleagueCli) && Boolean(fs.statSync(colleagueCli).mode & 0o111) ? 'pass' : codexKind,
  'Codex exact-continuity launcher',
  fs.existsSync(colleagueCli) ? colleagueCli : `run scripts/install-codex-integration.sh${codexNote}`,
);

result(fs.existsSync(path.join(root, 'node_modules', 'mammoth')) ? 'pass' : 'fail', 'Node dependencies', 'run npm install when missing');

if (failures) {
  process.stderr.write(`\n${failures} required check${failures === 1 ? '' : 's'} failed.\n`);
  process.exit(1);
}
process.stdout.write('\nColleague AI is ready for a local meeting test.\n');
