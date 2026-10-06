# Contributing (humans and coding agents)

Read these before proposing product, architecture, or runtime changes:

| Document | Purpose |
| --- | --- |
| [README.md](README.md) | What Smitline does, how to get started, safety, limitations |
| [docs/development.md](docs/development.md) | Surfaces, project layout, running from a checkout, tests |
| [docs/security.md](docs/security.md) | Security model, data and privacy |
| [docs/meetings.md](docs/meetings.md) | Joining meetings, the meetings console, local interfaces |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common problems and fixes |
| [docs/product-vision-and-progress.md](docs/product-vision-and-progress.md) | Canonical product brief, settled decisions, progress ledger |
| [docs/architecture.md](docs/architecture.md) | Runtime layout, sequences, retention and deletion |
| [docs/calls.md](docs/calls.md) | Call API: briefs, lifecycle, results, costs, delivery, hooks, server mode |
| [docs/phone.md](docs/phone.md) | Phone calls through SignalWire or Twilio and GPT-Live: setup, gateway, tunnel, settings |
| [docs/agents.md](docs/agents.md) | Connecting agents: local MCP, CLI, REST/SDKs, and the remote connector for cloud agents |
| [SETUP.md](SETUP.md) | Agent-driven setup that users start by pasting one prompt |
| [docs/capabilities.md](docs/capabilities.md) | Meeting platform capability matrix |
| [docs/meeting-adapters.md](docs/meeting-adapters.md) | Zoom, Teams, and Meet adapter behavior |
| [docs/control-panel.md](docs/control-panel.md) | Local console: Meetings, Calls, and Account |
| [docs/product-roadmap.md](docs/product-roadmap.md) | Near-term milestones |

Agent skills for setup and for placing calls live under [`.agents/skills/`](.agents/skills/).

## Invariants

- **GPT-Live is the only voice.** Nothing else listens or speaks in a call or meeting.
- **Brief in, result out.** Phone calls and meetings start through the calls API and return the same result shape.
- **Fail closed** on unknown fields, unsupported meeting links, incomplete briefs, and missing configuration.
- **Local-first:** loopback daemon, gitignored secrets, transcripts, call records, profiles, and context.
- **Disclose.** Phone calls say they are an AI acting for the named person during the call: the opening names the person and the reason, and the disclosure follows at a natural moment, before the goodbye at the latest. Meetings open with a disclosure unless the owner sets `COLLEAGUE_MEETING_INTRO=0`.
- **Preserve** existing Zoom/Teams/Meet admission, audio, mute, transcript, handoff, and console behavior unless a change explicitly replaces it.

## Tests

```bash
docker compose -f compose.meeting.yaml build meeting-agent
docker run --rm \
  --entrypoint /app/.venv/bin/python \
  -v "$PWD/meeting-runtime:/meeting-runtime:ro" \
  -v "$PWD/joinly:/opt/joinly:ro" \
  smitline-meeting:local \
  -m unittest discover -s /meeting-runtime -p 'test_*.py'

npm test
cd packages/sdk-python && python3 -m unittest discover -s tests
```

Do not commit `.env`, `.colleague/`, recordings, browser profiles, credentials, or other gitignored runtime state.
