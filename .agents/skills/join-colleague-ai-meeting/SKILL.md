---
name: join-colleague-ai-meeting
description: Join Zoom, Microsoft Teams, or Google Meet from the active Codex task with exact session continuity. Use when the user asks Codex or Colleague AI to join, participate in, monitor, or leave a live meeting while carrying the current coding context.
---

# Join a meeting from the current Codex task

Use the Colleague AI MCP tools directly. Do not open or fill the local control panel.

Before joining:

1. Run `printenv CODEX_THREAD_ID` in the current task command environment. Treat an empty value, `last`, `latest`, `--last`, or `local-portal` as an error. Never invent, infer, or reuse another task ID.
2. Resolve the current workspace with `pwd -P`.
3. Build a bounded `ContextHandoff` from information visible in the conversation and workspace. Include version `1`, objective, current task, concise summary, decisions, constraints, open questions, important files, recent conversation, and Git state when useful. Do not include hidden reasoning, credentials, or unrelated conversation.
4. Call `join_current_meeting` with the exact thread ID as `sessionId`, the absolute workspace, meeting URL, context, and permissions justified by the user's request. Omit permissions to use the safe defaults when the user has not authorized broader access.

Exact continuity is required for this workflow. If the task ID is unavailable or the exact join fails, report the blocker. Do not silently retry with `start_meeting`, `continuity: context`, `local-portal`, `last`, or `latest`.

After the tool returns a meeting ID, tell the user Colleague AI is joining and keep this task available for the runtime's delegated Codex turns. The runtime owns the session lease until the meeting ends or is cancelled. Use the meeting status, approval, artifact, cancellation, and handoff tools with that explicit meeting ID. Do not start a duplicate meeting for the same request.

When the meeting ends, retrieve the durable handoff if necessary and verify it targets the same session. Surface partial or failed finalization rather than claiming the handoff succeeded.
