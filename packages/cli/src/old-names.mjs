// Settings and files from before the product was called Smitline (it started as Colleague AI).
// The same rules as meeting-runtime/old_names.py: COLLEAGUE_* variables set, and win over, their
// SMITLINE_* names; migrateData renames old keys in .env files and moves .colleague to
// .smitline, leaving a link so an older image still finds it.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const OLD_PREFIX = 'COLLEAGUE_';
const NEW_PREFIX = 'SMITLINE_';
const ASSIGNMENT = /^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=.*)$/;

export function adoptOldSettings(env = process.env) {
  for (const key of Object.keys(env)) {
    if (key.startsWith(OLD_PREFIX)) env[NEW_PREFIX + key.slice(OLD_PREFIX.length)] = env[key];
  }
  return env;
}

/** The .env text with COLLEAGUE_* keys renamed; a key already set under its new name is dropped. */
export function renamedEnvText(text) {
  const lines = String(text).split(/(?<=\n)/);
  const keys = new Set(lines.map((line) => line.replace(/\r?\n$/, '').match(ASSIGNMENT)?.[2]).filter(Boolean));
  return lines.flatMap((line) => {
    const match = line.replace(/\r?\n$/, '').match(ASSIGNMENT);
    if (!match || !match[2].startsWith(OLD_PREFIX)) return [line];
    const key = NEW_PREFIX + match[2].slice(OLD_PREFIX.length);
    if (keys.has(key)) return [];
    return [match[1] + key + line.slice(match[1].length + match[2].length)];
  }).join('');
}

function rewrite(file) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch {
    return false;
  }
  const renamed = renamedEnvText(text);
  if (renamed === text) return false;
  const mode = fs.statSync(file).mode & 0o777;
  const tmp = `${file}.${crypto.randomBytes(4).toString('hex')}.tmp`;
  fs.writeFileSync(tmp, renamed, { mode: mode || 0o600 });
  fs.renameSync(tmp, file);
  return true;
}

/** Rename old keys in ROOT's .env files and move ROOT/.colleague to ROOT/.smitline. */
export function migrateData(root) {
  const changed = ['.env', '.env.meeting'].filter((name) => rewrite(path.join(root, name)));
  const oldDir = path.join(root, '.colleague');
  const newDir = path.join(root, '.smitline');
  try {
    if (fs.lstatSync(oldDir).isDirectory() && !fs.existsSync(newDir)) {
      fs.renameSync(oldDir, newDir);
      fs.symlinkSync('.smitline', oldDir);
      changed.push('.colleague');
    }
  } catch {
    // No old folder.
  }
  return changed;
}
