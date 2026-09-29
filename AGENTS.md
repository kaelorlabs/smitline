# Contributing (humans and coding agents)

Read these before proposing product, architecture, or runtime changes:

| Document | Purpose |
| --- | --- |
| [README.md](README.md) | Setup, security model, daemon/portal/SDK/CLI/MCP, continuity modes, troubleshooting |
| [docs/product-vision-and-progress.md](docs/product-vision-and-progress.md) | Canonical product brief, settled decisions, progress ledger |
| [docs/architecture.md](docs/architecture.md) | Runtime layout, sequences, retention and deletion |
| [docs/calls.md](docs/calls.md) | Call API: briefs, lifecycle, results, delivery, hooks, server mode |
| [docs/capabilities.md](docs/capabilities.md) | Platform and provider capability matrices |
| [docs/coding-providers.md](docs/coding-providers.md) | Codex, Cursor, and Claude Code integration rules |
| [docs/meeting-adapters.md](docs/meeting-adapters.md) | Zoom, Teams, and Meet adapter behavior |
| [docs/control-panel.md](docs/control-panel.md) | Local operations console |
| [docs/hosted-runtime.md](docs/hosted-runtime.md) | Runner pairing foundation (not production hosting) |
| [docs/developer-platform-implementation-plan.md](docs/developer-platform-implementation-plan.md) | Historical design notes; code and tests override stale baseline text |
| [docs/product-roadmap.md](docs/product-roadmap.md) | Near-term milestones |

Agent skills for local setup and meeting join live under [`.agents/skills/`](.agents/skills/).

## Invariants

- **Exact continuity** requires a real host-supplied session id. Never use `last`, `latest`, URL hashes, or invented ids.
- **Fail closed** on unknown providers, undocumented CLI flags, permission escalation, and missing capabilities.
- **Local-first:** loopback daemon, gitignored secrets, transcripts, jobs, profiles, and artifacts.
- **Speech is not authorization.** Side effects need typed permissions and operator approval.
- **Preserve** existing Zoom/Teams/Meet admission, audio, mute, transcript, handoff, and portal behavior unless a change explicitly replaces it.

## Tests

```bash
docker run --rm \
  --entrypoint /app/.venv/bin/python \
  -v "$PWD/meeting-runtime:/meeting-runtime:ro" \
  meeting-agent-joinly-login:local \
  -m unittest discover -s /meeting-runtime -p 'test_*.py'

npm test
python3 -m unittest discover -s packages/sdk-python/tests -p 'test_*.py'
```

Do not commit `.env`, `.colleague/`, recordings, browser profiles, credentials, or other gitignored runtime state.
