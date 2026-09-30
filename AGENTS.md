# Contributing (humans and coding agents)

Read these before proposing product, architecture, or runtime changes:

| Document | Purpose |
| --- | --- |
| [README.md](README.md) | What Colleague AI does, setup, security model, surfaces, tests, troubleshooting |
| [docs/product-vision-and-progress.md](docs/product-vision-and-progress.md) | Canonical product brief, settled decisions, progress ledger |
| [docs/architecture.md](docs/architecture.md) | Runtime layout, sequences, retention and deletion |
| [docs/calls.md](docs/calls.md) | Call API: briefs, lifecycle, results, costs, delivery, hooks, server mode |
| [docs/phone.md](docs/phone.md) | Phone calls through SignalWire or Twilio and GPT-Live: setup, gateway, tunnel, settings |
| [docs/agents.md](docs/agents.md) | Connecting agents: local MCP, CLI, REST/SDKs, and the remote connector for cloud agents |
| [SETUP.md](SETUP.md) | Agent-driven setup that users start by pasting one prompt |
| [docs/capabilities.md](docs/capabilities.md) | Meeting platform capability matrix |
| [docs/meeting-adapters.md](docs/meeting-adapters.md) | Zoom, Teams, and Meet adapter behavior |
| [docs/control-panel.md](docs/control-panel.md) | Local console: Meetings and Calls |
| [docs/product-roadmap.md](docs/product-roadmap.md) | Near-term milestones |

Agent skills for setup and for placing calls live under [`.agents/skills/`](.agents/skills/).

## Invariants

- **GPT-Live is the only voice.** Nothing else listens or speaks in a call or meeting.
- **Brief in, result out.** Phone calls and meetings start through the calls API and return the same result shape.
- **Fail closed** on unknown fields, unsupported meeting links, incomplete briefs, and missing configuration.
- **Local-first:** loopback daemon, gitignored secrets, transcripts, call records, profiles, and context.
- **Disclose.** Phone calls always open with an AI disclosure naming the person Colleague AI acts for. Meetings do too unless the owner sets `COLLEAGUE_MEETING_INTRO=0`.
- **Preserve** existing Zoom/Teams/Meet admission, audio, mute, transcript, handoff, and console behavior unless a change explicitly replaces it.

## Tests

```bash
docker compose -f compose.meeting.yaml build meeting-agent
docker run --rm \
  --entrypoint /app/.venv/bin/python \
  -v "$PWD/meeting-runtime:/meeting-runtime:ro" \
  -v "$PWD/joinly:/opt/joinly:ro" \
  colleague-meeting:local \
  -m unittest discover -s /meeting-runtime -p 'test_*.py'

npm test
cd packages/sdk-python && python3 -m unittest discover -s tests
```

Do not commit `.env`, `.colleague/`, recordings, browser profiles, credentials, or other gitignored runtime state.
