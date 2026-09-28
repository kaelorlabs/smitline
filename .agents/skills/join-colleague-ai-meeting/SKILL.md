---
name: join-colleague-ai-meeting
description: Join Zoom, Microsoft Teams, or Google Meet from the active Codex task with exact session continuity. Use when the user asks Codex or Colleague AI to join, participate in, monitor, or leave a live meeting while carrying the current coding context.
---

# Join a meeting from the current Codex task

Launch Colleague AI from the active task with the installed CLI. Do not open or fill the local control panel, and do not use the MCP `join_current_meeting` tool for launch. A persistent MCP process does not reliably receive the invoking task's environment; the CLI process does.

The context handoff is sent to the local daemon and meeting-relevant portions may be sent to GPT-Live/OpenAI after admission. Keep it minimal. Do not put `CODEX_THREAD_ID`, absolute workspace paths, credentials, untracked file names, or unrelated conversation in the context file. The launcher transfers the task id and workspace separately.

Before joining:

1. Confirm `CODEX_THREAD_ID` is present in the current task command environment without printing its value. Treat an empty value, `last`, `latest`, `--last`, or `local-portal` as an error. Never invent, infer, expose, or reuse another task ID.
2. Run the remaining commands from the current project directory. The CLI resolves the workspace internally; do not place its absolute path in the command line.
3. Build a bounded `ContextHandoff` JSON file from information visible in the conversation and workspace. Store it in a private temporary file with mode `0600`. Use this exact shape; `git` may be omitted:

   ```json
   {
     "version": 1,
     "objective": "Help the team with the current implementation.",
     "currentTask": "Diagnose and implement the requested change.",
     "summary": "Concise meeting-relevant context only.",
     "decisions": [],
     "constraints": [],
     "openQuestions": [],
     "importantFiles": ["repo/relative/file.ext"],
     "recentConversation": [
       { "role": "user", "text": "Meeting-relevant request." },
       { "role": "assistant", "text": "Concise response or current state." }
     ],
     "git": { "branch": "branch-name", "commit": "commit-sha", "dirty": false }
   }
   ```

   `currentTask` is a string. Every recent turn has exactly `role` (`user` or `assistant`) and `text`. `git` allows only `branch`, `commit`, and `dirty`; omit it when those values are unnecessary.
4. Validate the file locally before requesting daemon access:

   ```bash
   "${CODEX_HOME:-$HOME/.codex}/bin/colleague" context validate --file "<temporary ContextHandoff path>"
   ```

5. Run this command from the active project directory, without `--wait`:

   ```bash
   "${CODEX_HOME:-$HOME/.codex}/bin/colleague" join --meeting "<invite URL>" --context-file "<temporary ContextHandoff path>"
   ```

   The launcher inherits the exact `CODEX_THREAD_ID` and sends it to the daemon. Delete the temporary context file after the command returns. Do not put the context JSON directly in shell arguments.

Colleague AI's microphone policy is automatic: it joins muted, unmutes the platform microphone once when its voice session starts, opens with a short AI disclosure naming the user, then listens. Between replies a local audio gate transmits silence while the platform shows it unmuted; it mutes the platform microphone when the session ends. A host or participant mute is respected, and in Zoom it accepts the host's explicit "Ask to unmute" request. There is no separate always-unmuted launch mode. If the user asks to join unmuted, explain this behavior; do not encode an unsupported audio mode in context.

Exact continuity is required for this workflow. The launch needs access to the local loopback daemon; when the execution environment blocks `127.0.0.1`, request that access once after context validation. If the task ID or launcher is unavailable, or the exact join fails, report the blocker. Do not silently retry with MCP launch, `--context-continuity`, `start_meeting`, `local-portal`, `last`, or `latest`.

If another meeting is active, report its id and ask whether to replace it unless the user already instructed you to switch meetings. For an authorized switch, retry with `--replace`; this cancels the active meeting cleanly before joining the new one.

After the command returns a meeting ID, tell the user Colleague AI is joining and leave this task idle so the runtime can resume it for delegated Codex turns. Do not use `--wait` for an agent-native launch because the foreground Codex turn must finish before the runtime resumes the same task. The runtime owns the session lease until the meeting ends or is cancelled. MCP or CLI status, approval, artifact, cancellation, and handoff controls may be used with the explicit meeting ID. Do not start a duplicate meeting for the same request.

When the meeting ends, retrieve the durable handoff if necessary and verify it targets the same session. Surface partial or failed finalization rather than claiming the handoff succeeded.
