# Third-party source

This repository includes source snapshots, not nested Git repositories. Their original license files are retained.

| Directory | Upstream | Commit | License |
| --- | --- | --- | --- |
| `joinly/` | https://github.com/joinly-ai/joinly | `4ea1259de329aa6965d9fdb806db64a7cdeeb683` | MIT; see `joinly/LICENSE` |
| `agents-everywhere-starter-kit/` | https://github.com/CopilotKit/agents-everywhere-starter-kit | `a997712275a50eb3178e96224266bc6803b5c5c8` | MIT; see `agents-everywhere-starter-kit/LICENSE` |

Joinly modifications in this checkpoint: persistent browser-profile support, browser cleanup, optional guest-name entry for signed-in Google sessions, longer join-state wait, and explicit guest-rejection diagnostics.

The CopilotKit starter snapshot is unchanged and retained as a hackathon reference. The live voice interface does not yet integrate CopilotKit. Model weights are downloaded by the upstream Docker build; model and dependency licenses remain those of their respective publishers.
