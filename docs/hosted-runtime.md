# Hosted runtime foundation

This document describes the **foundation** control-plane and runner-transport contract. It is **not production hosting**.

Local loopback (`127.0.0.1` with a bearer token) remains the only supported production path. Credentials, browser profiles, workspace bytes, transcripts, screenshots, and coding-agent session handles stay on the local runner. A remote or mock plane may store tenant, user, and device metadata, safe lifecycle state, encrypted routing identifiers, and retention records.

Do not bind the daemon to a public address. `require_loopback_bind` rejects non-loopback hosts. There is no live certificate authority and no public TLS listener in this foundation.

## Protocol v1

`ControlPlane` and `RunnerTransport` are versioned at `protocolVersion: 1`. Message kinds are:

- `runner.register`
- `runner.capabilities`
- `job.assign`
- `job.cancel`
- `runner.heartbeat`
- `event.upload`
- `runner.reconnect`
- `handoff.final`
- `audit.record`

The default transport is loopback identity. `FakeTlsSession` is an explicit test double: HMAC-SHA256 MAC plus HMAC keystream XOR, with nonce/sequence replay protection and a key-rotation hook. It is **not** a TLS cipher and must not be used on a public network.

## Pairing

Pairing issues a short-lived, single-use code (default TTL 120 seconds). Completing pairing creates a revocable device identity. The pairing code and `deviceEnrollment` are revealed once. They are never stored in URLs, query strings, logs, events, errors, or `GET /v1/runner` status. The local runner persists only hashes under `hosted/runner-state.json` with mode `0600`.

Revoking a device unpaired the runner. Local loopback continues to work.

## Job binding and permissions

Remote jobs must bind `userId`, `deviceId`, provider, `sessionId`, `workspaceIdentity`, permissions, and `meetingId`. Missing fields are rejected. `workspaceIdentity` is an opaque `ws-` hash, never a filesystem path.

A hosted request may only **narrow** local permissions. The local runner is the final enforcement point. Arbitrary shell, filesystem path retrieval, credential retrieval, browser-profile download, transcript dump, screenshot dump, and secret-bearing errors are forbidden over remote transport.

Offline reconnect is cursor-based and idempotent. Duplicate assignment, approval, commit, push, and handoff identifiers do not create a second meeting or append a second handoff.

## Remote-safe events

Lifecycle, state, capability, and audit events may upload. Transcript text, artifact bytes, screenshots, raw paths, and credentials must not. Artifact metadata may include `id`, `type`, `size`, and `createdAt`.

## Surfaces

- Daemon: `GET /v1/runner`, `POST /v1/runner/pair`, `POST /v1/runner/pair/complete`, `POST /v1/runner/unpair`
- CLI: `colleague runner status|pair|complete|unpair`
- SDK: `runnerStatus`, `pairRunner`, `completeRunnerPair`, `unpairRunner`
- MCP: `get_runner_status`, `pair_runner`, `complete_runner_pair`, `unpair_runner`
- Portal: Connect-runner analog next to account connection. The pairing code is shown once and never again.
