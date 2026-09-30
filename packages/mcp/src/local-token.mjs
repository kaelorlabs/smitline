// The bearer token local agents send to the console's MCP endpoint
// (http://127.0.0.1:8095/mcp). It lives in <COLLEAGUE_ROOT>/.colleague/mcp.token,
// readable only by its owner, and is made on first need.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const LOCAL_MCP_TOKEN_FILE = 'mcp.token';

export function localMcpTokenPath(root) {
  return path.join(root, '.colleague', LOCAL_MCP_TOKEN_FILE);
}

function readToken(file) {
  try {
    return fs.readFileSync(file, 'utf8').trim();
  } catch (error) {
    if (error.code === 'ENOENT') return '';
    throw error;
  }
}

/** The local MCP token, created (0600, in a 0700 directory) when there is none yet. */
export function readOrCreateLocalMcpToken(root) {
  const file = localMcpTokenPath(root);
  const existing = readToken(file);
  if (existing) return existing;
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  try { fs.chmodSync(dir, 0o700); } catch { /* not ours to change */ }
  const token = crypto.randomBytes(32).toString('base64url');
  // Written aside and linked into place, so a concurrent reader never sees a half-written
  // file and two first uses agree on one token.
  const temporary = `${file}.${crypto.randomBytes(6).toString('hex')}.tmp`;
  fs.writeFileSync(temporary, `${token}\n`, { mode: 0o600 });
  try {
    fs.linkSync(temporary, file);
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const winner = readToken(file);
    if (winner) return winner;
    // An empty file left behind: replace it.
    fs.renameSync(temporary, file);
    fs.chmodSync(file, 0o600);
    return token;
  } finally {
    try { fs.unlinkSync(temporary); } catch { /* already moved */ }
  }
  fs.chmodSync(file, 0o600);
  return token;
}
