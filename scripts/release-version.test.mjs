import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { VERSION_FILES, bumpVersion, checkVersions, currentVersion } from './release-version.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/** A copy of the version files and the changelog, to bump without touching the checkout. */
function copy(t, changelog) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'smitline-release-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  for (const file of Object.keys(VERSION_FILES)) {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.copyFileSync(path.join(ROOT, file), path.join(root, file));
  }
  fs.writeFileSync(path.join(root, 'CHANGELOG.md'), changelog);
  return root;
}

const next = (version) => version.replace(/\d+$/, (patch) => String(Number(patch) + 1));

test('every file that carries the version carries package.json\'s version', () => {
  assert.deepEqual(checkVersions(), []);
});

test('a bump moves every version file and dates the Unreleased section', (t) => {
  const from = currentVersion();
  const to = next(from);
  const root = copy(t, `# Changelog\n\n## Unreleased\n\n- **Something new.**\n\n## ${from} — 2026-11-01\n\n- Older.\n`);
  const written = bumpVersion(to, { root, date: '2026-11-20' });
  assert.equal(written.length, Object.keys(VERSION_FILES).length + 1);
  assert.equal(currentVersion(root), to);
  assert.deepEqual(checkVersions(root), []);
  const changelog = fs.readFileSync(path.join(root, 'CHANGELOG.md'), 'utf8');
  assert.match(changelog, new RegExp(`^## ${to.replaceAll('.', '\\.')} — 2026-11-20$`, 'm'));
  assert.match(changelog, new RegExp(`^## ${from.replaceAll('.', '\\.')} — 2026-11-01$`, 'm')); // the old heading stays
  assert.doesNotMatch(changelog, /## Unreleased/);
});

test('a bump refuses a wrong version, an empty changelog, and files that already disagree', (t) => {
  const from = currentVersion();
  const ready = `# Changelog\n\n## Unreleased\n\n- **Something new.**\n`;
  const root = copy(t, ready);
  assert.throws(() => bumpVersion('1.0', { root }), /not a version/);
  assert.throws(() => bumpVersion(from, { root }), /not after the current version/);
  assert.throws(() => bumpVersion(next(from), { root, date: '20 Nov' }), /not a date/);

  fs.writeFileSync(path.join(root, 'CHANGELOG.md'), `# Changelog\n\n## Unreleased\n\n## ${from} — 2026-11-01\n`);
  assert.throws(() => bumpVersion(next(from), { root }), /empty/);
  fs.writeFileSync(path.join(root, 'CHANGELOG.md'), `# Changelog\n\n## ${from} — 2026-11-01\n`);
  assert.throws(() => bumpVersion(next(from), { root }), /exactly one "## Unreleased"/);

  fs.writeFileSync(path.join(root, 'CHANGELOG.md'), ready);
  fs.writeFileSync(path.join(root, 'server.json'), fs.readFileSync(path.join(root, 'server.json'), 'utf8').replace(from, '0.0.1'));
  assert.deepEqual(checkVersions(root), [`server.json: ${from} 1 times, expected 2`]);
  assert.throws(() => bumpVersion(next(from), { root }), /disagree before the bump/);
  assert.equal(currentVersion(root), from); // nothing was written
});
