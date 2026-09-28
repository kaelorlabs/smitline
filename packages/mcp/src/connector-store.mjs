// Registered OAuth clients and grants for the remote connector. Tokens are
// stored only as SHA-256 digests, in private files under .colleague/connector/.
// The files are re-read when they change on disk, so `colleague connector
// revoke` takes effect on a running connector.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const ACCESS_TTL_MS = 60 * 60_000;
export const REFRESH_TTL_MS = 30 * 24 * 60 * 60_000;
export const MAX_CLIENTS = 200;
// A client that registered recently may be in the middle of its approval.
const REGISTRATION_GRACE_MS = 10 * 60_000;

export function sha256(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex');
}

export function connectorDir(root) {
  return path.join(root, '.colleague', 'connector');
}

function own(table, key) {
  return typeof key === 'string' && Object.hasOwn(table, key) ? table[key] : undefined;
}

function iso(ms) {
  return new Date(ms).toISOString();
}

function jsonFile(file, empty) {
  let data = null;
  let stamp = null;
  const stat = () => {
    try {
      const info = fs.statSync(file, { bigint: true });
      return `${info.ino}:${info.mtimeNs}:${info.size}`;
    } catch {
      return 'missing';
    }
  };
  return {
    read() {
      const current = stat();
      if (data && current === stamp) return data;
      try {
        data = { ...empty(), ...JSON.parse(fs.readFileSync(file, 'utf8')) };
      } catch {
        // Missing or unreadable: start empty, which fails closed for tokens.
        data = empty();
      }
      stamp = current;
      return data;
    },
    write(next) {
      const dir = path.dirname(file);
      try {
        fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
        fs.chmodSync(dir, 0o700);
        const tmp = `${file}.${crypto.randomBytes(4).toString('hex')}.tmp`;
        fs.writeFileSync(tmp, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
        fs.renameSync(tmp, file);
        fs.chmodSync(file, 0o600);
      } catch (error) {
        data = null;
        throw error;
      }
      data = next;
      stamp = stat();
    },
  };
}

export function openConnectorStore(root, { now = Date.now } = {}) {
  const dir = connectorDir(root);
  const clientsFile = jsonFile(path.join(dir, 'clients.json'), () => ({ version: 1, clients: {} }));
  const tokensFile = jsonFile(path.join(dir, 'tokens.json'), () => ({
    version: 1, grants: {}, access: {}, refresh: {}, retired: {},
  }));

  function prune(db) {
    const at = now();
    for (const table of ['access', 'refresh', 'retired']) {
      for (const [digest, entry] of Object.entries(db[table])) {
        if (entry.expiresAt <= at) delete db[table][digest];
      }
    }
    const live = new Set([...Object.values(db.access), ...Object.values(db.refresh)].map((entry) => entry.grantId));
    for (const grantId of Object.keys(db.grants)) {
      if (!live.has(grantId)) delete db.grants[grantId];
    }
    return db;
  }

  function mint(db, grantId, withRefresh) {
    const at = now();
    const accessToken = `cai_at_${crypto.randomBytes(32).toString('base64url')}`;
    db.access[sha256(accessToken)] = { grantId, expiresAt: at + ACCESS_TTL_MS };
    const tokens = { accessToken, expiresIn: ACCESS_TTL_MS / 1000 };
    if (withRefresh) {
      const refreshToken = `cai_rt_${crypto.randomBytes(32).toString('base64url')}`;
      db.refresh[sha256(refreshToken)] = { grantId, expiresAt: at + REFRESH_TTL_MS };
      tokens.refreshToken = refreshToken;
    }
    return tokens;
  }

  function dropGrant(db, grantId) {
    let tokens = 0;
    for (const table of ['access', 'refresh', 'retired']) {
      for (const [digest, entry] of Object.entries(db[table])) {
        if (entry.grantId !== grantId) continue;
        delete db[table][digest];
        if (table !== 'retired') tokens += 1;
      }
    }
    delete db.grants[grantId];
    return tokens;
  }

  function activeClientIds() {
    const db = tokensFile.read();
    const at = now();
    const ids = new Set();
    for (const entry of [...Object.values(db.access), ...Object.values(db.refresh)]) {
      const grant = own(db.grants, entry.grantId);
      if (grant && entry.expiresAt > at) ids.add(grant.clientId);
    }
    return ids;
  }

  return {
    dir,

    /** Register a public client; returns null when the registry is full. */
    registerClient({ clientName, redirectUris, grantTypes }) {
      const db = clientsFile.read();
      const ids = Object.keys(db.clients);
      if (ids.length >= MAX_CLIENTS) {
        const active = activeClientIds();
        const cutoff = now() - REGISTRATION_GRACE_MS;
        const idle = ids
          .filter((id) => !active.has(id) && Date.parse(db.clients[id].createdAt) < cutoff)
          .sort((a, b) => Date.parse(db.clients[a].createdAt) - Date.parse(db.clients[b].createdAt));
        if (!idle.length) return null;
        delete db.clients[idle[0]];
      }
      const clientId = `cai-${crypto.randomBytes(16).toString('hex')}`;
      const record = { clientId, clientName, redirectUris, grantTypes, createdAt: iso(now()) };
      db.clients[clientId] = record;
      clientsFile.write(db);
      return record;
    },

    getClient(clientId) {
      return own(clientsFile.read().clients, clientId);
    },

    createGrant({ clientId, resource, scope, refresh }) {
      const db = prune(tokensFile.read());
      const grantId = `grant-${crypto.randomBytes(8).toString('hex')}`;
      db.grants[grantId] = { clientId, resource, scope, createdAt: iso(now()), refreshedAt: iso(now()) };
      const tokens = mint(db, grantId, refresh);
      tokensFile.write(db);
      return { grantId, ...tokens };
    },

    /** Resolve an access token to its grant, or null. */
    checkAccess(token) {
      const db = tokensFile.read();
      const entry = own(db.access, sha256(token));
      if (!entry || entry.expiresAt <= now()) return null;
      const grant = own(db.grants, entry.grantId);
      if (!grant) return null;
      return { grantId: entry.grantId, clientId: grant.clientId, resource: grant.resource, scope: grant.scope };
    },

    /** Exchange a refresh token for new tokens. Reusing a rotated token revokes its grant. */
    rotateRefresh(token, clientId) {
      const db = prune(tokensFile.read());
      const digest = sha256(token);
      const entry = own(db.refresh, digest);
      if (!entry) {
        const retired = own(db.retired, digest);
        if (!retired) return { error: 'invalid' };
        dropGrant(db, retired.grantId);
        tokensFile.write(db);
        return { error: 'reused', grantId: retired.grantId };
      }
      const grant = own(db.grants, entry.grantId);
      if (!grant || grant.clientId !== clientId) return { error: 'invalid' };
      delete db.refresh[digest];
      db.retired[digest] = { grantId: entry.grantId, expiresAt: entry.expiresAt };
      grant.refreshedAt = iso(now());
      const tokens = mint(db, entry.grantId, true);
      tokensFile.write(db);
      return { grantId: entry.grantId, resource: grant.resource, scope: grant.scope, ...tokens };
    },

    revokeGrant(grantId) {
      const db = tokensFile.read();
      if (!own(db.grants, grantId)) return 0;
      const tokens = dropGrant(db, grantId);
      tokensFile.write(db);
      return tokens;
    },

    /** Revoke every grant, or those of one client. Registrations stay. */
    revoke({ all = false, clientId } = {}) {
      const db = prune(tokensFile.read());
      const grantIds = Object.entries(db.grants)
        .filter(([, grant]) => all || grant.clientId === clientId)
        .map(([grantId]) => grantId);
      let tokens = 0;
      for (const grantId of grantIds) tokens += dropGrant(db, grantId);
      if (grantIds.length) tokensFile.write(prune(db));
      return { grants: grantIds.length, tokens };
    },

    /** Clients and active grants, without any token material. */
    summary() {
      const clients = clientsFile.read().clients;
      const db = tokensFile.read();
      const at = now();
      const grants = [];
      for (const [grantId, grant] of Object.entries(db.grants)) {
        const live = (table) => Object.values(db[table]).filter((entry) => entry.grantId === grantId && entry.expiresAt > at);
        const access = live('access');
        const refresh = live('refresh');
        if (!access.length && !refresh.length) continue;
        grants.push({
          grantId,
          clientId: grant.clientId,
          clientName: own(clients, grant.clientId)?.clientName ?? null,
          createdAt: grant.createdAt,
          refreshedAt: grant.refreshedAt,
          activeAccessTokens: access.length,
          expiresAt: iso(Math.max(...[...access, ...refresh].map((entry) => entry.expiresAt))),
        });
      }
      return {
        clients: Object.values(clients).map((client) => ({
          clientId: client.clientId,
          clientName: client.clientName,
          redirectUris: client.redirectUris,
          createdAt: client.createdAt,
          activeGrants: grants.filter((grant) => grant.clientId === client.clientId).length,
        })),
        grants,
      };
    },
  };
}
