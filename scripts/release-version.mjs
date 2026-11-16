#!/usr/bin/env node
// Moves Smitline to a new version for a release (docs/releasing.md): every file that carries the
// version, and the changelog's Unreleased section, which becomes the version and the date. It
// checks everything before it writes anything. --check, which npm test also runs, fails when
// the files disagree, so a half-done bump never reaches a tag.
//   node scripts/release-version.mjs 0.3.14 [--date 2026-11-20]
//   node scripts/release-version.mjs --check
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/** Every file that carries the version, and how many times. package.json is the source. */
export const VERSION_FILES = Object.freeze({
  'package.json': 1,
  'package-lock.json': 2,
  'server.json': 2,
  'SETUP.md': 1,
  'meeting-runtime/openapi.json': 1,
  'packages/cli/package.json': 1,
  'packages/mcp/package.json': 1,
  'packages/mcp/README.md': 1,
  'packages/mcpb/manifest.json': 1,
  'packages/mcpb/server/tools.json': 1,
  'packages/sdk-python/pyproject.toml': 1,
  'packages/sdk-python/README.md': 1,
  'packages/sdk-python/smitline/__init__.py': 1,
  'packages/sdk-python/smitline/client.py': 1,
  'packages/sdk-typescript/package.json': 1,
  'packages/sdk-typescript/README.md': 1,
  'packages/sdk-typescript/src/index.mjs': 1,
});

const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const UNRELEASED = /^## Unreleased[ \t]*$/m;

const read = (root, file) => fs.readFileSync(path.join(root, file), 'utf8');

/** A version as a whole word: 0.3.12, but not 10.3.12 or 0.3.120. */
function exactly(version) {
  return new RegExp(`(?<![0-9.])${version.replaceAll('.', '\\.')}(?![0-9])`, 'g');
}

function compare(a, b) {
  const [x, y] = [a, b].map((version) => version.split('.').map(Number));
  for (let i = 0; i < 3; i += 1) if (x[i] !== y[i]) return x[i] - y[i];
  return 0;
}

export function currentVersion(root = ROOT) {
  return JSON.parse(read(root, 'package.json')).version;
}

/** What is wrong with the version files: an empty list when they all carry package.json's version. */
export function checkVersions(root = ROOT) {
  const version = currentVersion(root);
  if (!VERSION.test(String(version))) return [`package.json: "${version}" is not a version like 0.3.13`];
  const problems = [];
  for (const [file, expected] of Object.entries(VERSION_FILES)) {
    const found = (read(root, file).match(exactly(version)) || []).length;
    if (found !== expected) problems.push(`${file}: ${version} ${found} times, expected ${expected}`);
  }
  return problems;
}

/** Moves every version file to next and dates the changelog's Unreleased section; returns the files written. */
export function bumpVersion(next, { root = ROOT, date = new Date().toISOString().slice(0, 10) } = {}) {
  const current = currentVersion(root);
  if (!VERSION.test(String(next))) throw new Error(`${next} is not a version like 0.3.13`);
  if (compare(next, current) <= 0) throw new Error(`${next} is not after the current version, ${current}`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error(`${date} is not a date like 2026-11-20`);
  const problems = checkVersions(root);
  if (problems.length) throw new Error(`The version files disagree before the bump:\n${problems.join('\n')}`);

  const changelog = read(root, 'CHANGELOG.md');
  const headings = changelog.match(new RegExp(UNRELEASED.source, 'gm')) || [];
  if (headings.length !== 1) throw new Error('CHANGELOG.md needs exactly one "## Unreleased" section, listing what this release changes');
  const section = changelog.slice(changelog.search(UNRELEASED)).split(/^## /m)[1];
  if (!/^- /m.test(section)) throw new Error('The Unreleased section of CHANGELOG.md is empty: there is nothing to release');

  const writes = { 'CHANGELOG.md': changelog.replace(UNRELEASED, `## ${next} — ${date}`) };
  for (const file of Object.keys(VERSION_FILES)) writes[file] = read(root, file).replace(exactly(current), next);
  for (const [file, text] of Object.entries(writes)) fs.writeFileSync(path.join(root, file), text);
  return Object.keys(writes);
}

function main(args) {
  if (args[0] === '--check') {
    const problems = checkVersions();
    if (problems.length) {
      console.error(`The version files disagree:\n${problems.join('\n')}`);
      process.exit(1);
    }
    console.log(`All ${Object.keys(VERSION_FILES).length} version files carry ${currentVersion()}.`);
    return;
  }
  const at = args.indexOf('--date');
  const date = at === -1 ? undefined : args[at + 1];
  const [next] = args.filter((arg, i) => !arg.startsWith('--') && i !== at + 1);
  if (!next) {
    console.error('Usage: node scripts/release-version.mjs <version> [--date YYYY-MM-DD] | --check');
    process.exit(2);
  }
  try {
    const from = currentVersion();
    const written = bumpVersion(next, date ? { date } : {});
    console.log(`${from} -> ${next}: ${written.length} files. Then run the tests (docs/releasing.md).`);
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main(process.argv.slice(2));
