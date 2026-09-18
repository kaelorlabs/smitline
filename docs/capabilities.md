# Capability matrices

Values below match the code. Optional CLIs are **feature-detected** from `--help`; missing binaries or undocumented flags stay unavailable. Colleague AI does not invent resume flags, model names, or Meet/CLI features.

Query live records with `GET /v1/providers`, `colleague providers`, MCP `list_coding_providers`, or the SDKs.

## Meeting platforms

From `MeetingPlatformAdapter.capabilities` in `meeting-runtime/adapters.py`. `text_chat` defaults true. `screen_sharing` (outgoing desktop share from this computer) is false on every adapter.

| Capability | Zoom | Teams | Google Meet |
| --- | --- | --- | --- |
| Official HTTPS invite only | `*.zoom.us` `/j/` or `/wc/join/` numeric ids | `teams.microsoft.com` / `teams.live.com` meetup or meet paths | `meet.google.com` `/xxx-yyyy-zzz` (3-4-3 letters) |
| Government / lookalike hosts | Rejected | Government Teams not enabled; lookalikes rejected | `www.`, workspace/stream hosts, `/landing`, `/new` rejected |
| Guest join first | Yes | Yes | Yes |
| Signed-in profile fallback | No | Microsoft (`teams-connected` / `teams`) | Google (`google-connected` / `google`) |
| Text chat send | Yes (not proof of recipient delivery) | Yes | Yes |
| File delivery into meeting chat | Experimental Zoom upload | Local save only | Local save only |
| Outgoing screen share from this computer | No | No | No |
| Incoming shared-content capture | Yes, opt-in, off by default | Yes, opt-in, off by default | Yes, opt-in, off by default |
| Virtual camera | Yes | Yes | Yes |
| Participant discovery | No | Yes | Yes |
| Empty-room auto-leave | Ended/removal detection only (no participant count) | Leaves when the toolbar reports one participant | Same count-based leave when the toolbar reports one participant; ended/removal detection also applies |

Live tenant policy can still refuse admission. Browser fixtures do not prove production compatibility.

## Coding-agent providers

From `meeting-runtime/providers/`. Enable **one** coding agent per meeting. Workspace mutations always go through approvals + the workspace executor, not through Cursor/Claude CLIs (`workspaceActions` is false on every provider).

| Capability | Codex | Cursor | Claude Code |
| --- | --- | --- | --- |
| Default | Yes | Optional | Optional |
| Binary | `codex` or `CODEX_BIN` | `cursor-agent` or `CURSOR_BIN` | `claude` or `CLAUDE_BIN` |
| Official login | `codex login` | CLI's documented login | `claude login` |
| Installed / usable | Installed iff binary found; usable iff installed | Installed iff binary found; usable iff `--print`/`--output-format` is in help | Same detection as Cursor |
| Exact session resume | Yes, with a real host `sessionId` | Only if help documents a resume flag **and** a noninteractive print flag | Same |
| Context continuity | Yes | Yes when noninteractive print is documented | Same |
| Structured progress | Yes | Yes when help documents JSON output | Same |
| Cancellation | Yes | Yes | Yes |
| Handoff append | Yes | Yes only when exact resume is available | Same |
| Workspace read | Yes | Yes | Yes |
| Workspace actions via the CLI | No | No | No |
| Supported models advertised | Codex allowlist in `codex_tool.py` | Not guessed; explicit model only with a documented flag and caller authorization | Not guessed |
| `reasonUnavailable` examples | `missing_binary` | `missing_binary`, `capability_unavailable`, `exact_resume_unsupported` | Same |

Exact continuity **requires a host-supplied real session id**. `last`, `latest`, `--last`, and meeting-URL hashes are rejected. The local portal never has that id and always uses context continuity.
