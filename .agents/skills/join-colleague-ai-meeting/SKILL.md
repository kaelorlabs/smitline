---
name: join-colleague-ai-meeting
description: Join Zoom, Microsoft Teams, or Google Meet from the active Codex task with exact session continuity. Use when the user asks Codex or Colleague AI to join, participate in, monitor, or leave a live meeting while carrying the current coding context.
---

# Join a meeting from the current Codex task

Launch Colleague AI from the active task with the installed CLI. Do not open or fill the local control panel, and do not use the MCP `join_current_meeting` tool for launch. A persistent MCP process does not reliably receive the invoking task's environment; the CLI process does.

Before joining:

1. Run `printenv CODEX_THREAD_ID` in the current task command environment. Treat an empty value, `last`, `latest`, `--last`, or `local-portal` as an error. Never invent, infer, or reuse another task ID.
2. Resolve the current workspace with `pwd -P`.
3. Build a bounded `ContextHandoff` JSON file from information visible in the conversation and workspace. Include `version: 1`, `objective`, `currentTask`, `summary`, `decisions`, `constraints`, `openQuestions`, `importantFiles`, `recentConversation`, and optional `git`. Do not include hidden reasoning, credentials, or unrelated conversation. Store it in a private temporary file with mode `0600`.
4. Run this command from the active project directory, without `--wait`:

   ```bash
   "${CODEX_HOME:-$HOME/.codex}/bin/colleague" join --meeting "<invite URL>" --workspace "$(pwd -P)" --context-file "<temporary ContextHandoff path>"
   ```

   The launcher inherits the exact `CODEX_THREAD_ID` and sends it to the daemon. Delete the temporary context file after the command returns. Do not put the context JSON directly in shell arguments.

Exact continuity is required for this workflow. If the task ID or launcher is unavailable, or the exact join fails, report the blocker. Do not silently retry with MCP launch, `--context-continuity`, `start_meeting`, `local-portal`, `last`, or `latest`.

After the command returns a meeting ID, tell the user Colleague AI is joining and leave this task idle so the runtime can resume it for delegated Codex turns. Do not use `--wait` for an agent-native launch because the foreground Codex turn must finish before the runtime resumes the same task. The runtime owns the session lease until the meeting ends or is cancelled. MCP or CLI status, approval, artifact, cancellation, and handoff controls may be used with the explicit meeting ID. Do not start a duplicate meeting for the same request.

When the meeting ends, retrieve the durable handoff if necessary and verify it targets the same session. Surface partial or failed finalization rather than claiming the handoff succeeded.
