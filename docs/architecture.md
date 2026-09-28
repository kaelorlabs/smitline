# Architecture

Colleague AI is a **local-first** meeting runtime. The loopback daemon on `127.0.0.1` is the supported production path. GPT-Live provides cloud voice. Coding-agent CLIs, browser profiles, workspace bytes, transcripts, and credentials stay on this computer.

See [capabilities](capabilities.md) for truthful platform and provider matrices. See [hosted runtime](hosted-runtime.md) for the foundation control plane (not production hosting).

## Runtime pieces

```text
Host integrations (Codex / Cursor / Claude MCP or CLI)
        │  real sessionId for exact continuity
        ▼
SDK / CLI / MCP  ──►  loopback daemon 127.0.0.1:8765
        │                    │
        │                    ├─ leases originating coding-agent session
        │                    ├─ approvals, artifacts, git broker
        │                    └─ starts meeting supervisor
        ▼
Local portal 127.0.0.1:8095  (context continuity: local-portal)
        │
        ▼
Docker meeting-agent  ← browser + PulseAudio + virtual camera
        │
        ▼
Zoom / Teams / Meet web client     GPT-Live (one gpt-live-1 session)
```

The meeting browser, virtual display, and audio bridge run in Docker (`compose.meeting.yaml`, service `meeting-agent`). Coding-agent workers run on the host so official CLI logins are not copied into the container.

## Join and admission

```mermaid
sequenceDiagram
    participant Caller as SDK/CLI/MCP/portal
    participant Daemon as Loopback daemon
    participant Super as Meeting supervisor
    participant Adapter as Platform adapter
    participant Meet as Zoom/Teams/Meet
    Caller->>Daemon: POST /v1/meetings (url, agentSession, permissions)
    Daemon->>Daemon: exclusive lease on agentSession.sessionId
    Daemon->>Super: start meeting
    Super->>Adapter: open invitation in local browser
    Adapter->>Meet: join (guest first)
    alt waiting room
        Adapter-->>Daemon: waiting_for_admission
        Meet->>Adapter: host admits
    end
    Adapter->>Meet: connect computer audio
    Super->>Super: start one gpt-live-1 session
    Adapter-->>Daemon: live
```

Portal joins always use `sessionId: local-portal` and `continuity: context`. Exact continuity is only for host integrations that pass the real originating thread id.

## Selective speech (unmute / remute)

```mermaid
sequenceDiagram
    participant Live as GPT-Live
    participant Gate as Virtual microphone gate
    participant Adapter as Platform adapter
    participant Meet as Meeting toolbar
    Note over Adapter,Meet: Platform mic stays connected; gate carries model audio
    Live->>Gate: generated speech
    Gate->>Adapter: transport speech (gate open)
    Adapter->>Meet: platform unmute if still locally muted
    Meet-->>Adapter: participants hear reply
    Gate->>Adapter: silence (gate closed)
    Adapter->>Meet: remute after playback
    Note over Meet: Host/participant mute is authoritative and is not auto-reopened
```

GPT-Live owns pauses, backchannels, and interruptions. The local runtime does not classify meeting speech or add a silence delay.

## Exact-session delegation and final handoff

```mermaid
sequenceDiagram
    participant Host as Coding-agent host
    participant Daemon as Loopback daemon
    participant Live as GPT-Live
    participant Provider as Codex/Cursor/Claude adapter
    Host->>Daemon: join with real sessionId (never last/latest)
    Daemon->>Daemon: lease session exclusive
    Live->>Daemon: client-delegated coding task
    Daemon->>Provider: resume exact thread when capability allows
    Provider-->>Live: bounded result for spoken delivery
    Note over Live: gpt-live-1 stays connected (store false)
    Note over Daemon,Provider: meeting end or cancel
    Daemon->>Provider: append MeetingHandoff once
    Daemon->>Daemon: release lease
    Provider-->>Host: same conversation continues
```

Context continuity (`continuity: context`, `sessionId: local-portal`) does not resume an originating thread. Cursor and Claude Code exact resume run only when the installed CLI help documents a resume flag.

## Approval-gated workspace action

```mermaid
sequenceDiagram
    participant Live as GPT-Live
    participant Daemon as Loopback daemon
    participant Op as Operator (portal/CLI/SDK)
    participant Exec as Workspace executor
    participant Git as Git broker
    Live->>Daemon: requested mutation/command
    Daemon->>Daemon: permission mode
    alt disabled
        Daemon-->>Live: denied
    else allowed
        Daemon->>Exec: run in isolated worktree
    else approval-required
        Daemon-->>Op: approval.required
        Op->>Daemon: approved or denied
        alt approved
            Daemon->>Exec: isolated worktree + artifacts
            opt commits/pushes also approval-required
                Exec->>Git: typed commit/push request
                Op->>Git: decide
            end
        end
    end
```

Mutations, tests, commits, and pushes never go through the Cursor or Claude CLIs. They stay on the generic approval plane, workspace executor, and git broker.

## Guest-then-signed-in fallback

```mermaid
sequenceDiagram
    participant Op as Operator
    participant Portal as Local portal
    participant Adapter as Zoom/Teams/Meet adapter
    participant IdP as Official Google/Microsoft page
    Op->>Portal: Connect Google or Microsoft account
    Portal->>Adapter: Open meeting view (noVNC)
    Op->>IdP: sign in yourself (no scripted passwords)
    Adapter->>Portal: write local *-connected marker
    Op->>Portal: stop account browser, start meeting
    Portal->>Adapter: join as isolated guest
    alt guest admitted
        Adapter-->>Portal: live
    else guest explicitly denied
        Adapter->>Adapter: retry once with signed-in profile
        alt admitted
            Adapter-->>Portal: live
        else still denied
            Adapter-->>Portal: authentication_required
        end
    end
```

Disconnect removes the local profile directory. It does not revoke sessions on other devices. Zoom has no signed-in profile fallback.

## Incoming shared-content change detection

When screen share is enabled, the meeting container screenshots the share surface every `captureIntervalMs`. Only settled, meaningfully changed frames reach the paid vision analyzer:

```text
meeting container (each capture)
  inbox still holds a frame        -> skip (backpressure)
  same bytes as previous capture   -> reuse its signature, no decode
  tile signature                   -> ~64x36 tiles (rows follow the aspect ratio); per tile the
                                      mean luminance and mean horizontal/vertical gradient over
                                      at most 6x6 sampled pixels
  settle                           -> must match the previous capture for settleTicks captures
  mask                             -> tiles that changed in 4 of the last 6 local comparisons
                                      (clock, video, webcam thumbnail, caret) are ignored until
                                      they stay quiet for about 3 captures
  changed vs last selected frame   -> write to inbox with the masked tile list
host daemon (each inbox frame)
  duplicate bytes / unchanged / oversized -> skip, as before
  matches one of the last 32 analyzed screens of this meeting
                                   -> re-emit that observation with reused: true; no analyzer
                                      call and no new screenshot or observation artifact
  otherwise                        -> store screenshot, analyze, store observation, remember it
container -> GPT-Live session.thinking.append ("Shared content: ...")
```

A tile counts as changed when its luminance or either gradient moves by more than 8 of 255. The change score is the square root of the changed-tile fraction, roughly the side of the changed area relative to the frame side.

| Setting | Default | Meaning |
| --- | --- | --- |
| `captureIntervalMs` | 4000 | Capture period, 2000-15000 ms. |
| `minChange` | 0.08 | Minimum change score. 0.08 is about 15 of 2304 tiles: a new slide bullet or a two-line scroll passes; a mouse pointer (1-4 tiles) or a hover highlight does not. |
| `settleTicks` | 1 | Consecutive near-identical captures needed before selection, 0-5. 0 selects the first changed capture, including mid-transition frames. |

Cost for a 1920x1080 frame in the meeting container: about 70 ms when the bytes changed (26 ms PNG decode through Pillow when importable, 45 ms tile sampling) and a SHA-256 otherwise. The host daemon decodes in pure Python (about 0.2 s for a slide, 0.8-0.9 s for dense code) and only for selected frames. Both sides compute signatures in a worker thread.

Content that keeps changing over more than half the frame, such as full-screen video, never settles and is not analyzed until it stops.

## Retention and deletion

All of these paths are gitignored. Stop the meeting or daemon before deleting files in use.

| Data | Location | How to delete |
| --- | --- | --- |
| Transcripts and call traces | `meeting-runtime/recordings/` | Delete the meeting directory from the portal history or remove the folder |
| Uploaded/pasted context | `meeting-runtime/context/index.json` | Portal **Clear saved context**, or delete the file |
| Browser profiles (Teams/Google) | `meeting-runtime/profiles/` | Portal **Disconnect**, which removes the local profile; or delete the directory |
| Coding-agent job files | `meeting-runtime/jobs/` (Codex at root; Cursor/Claude under `jobs/cursor`, `jobs/claude-code`) | Delete after workers are stopped; do not remove an active `worker.lock` to steal a session |
| Default empty workspace | `meeting-runtime/codex-workspace/` | Delete contents; the launcher recreates the directory |
| Daemon auth token | `.colleague/daemon.auth` | Stop the daemon; deleting the file forces a new token on next start |
| Daemon meetings, events, leases | `.colleague/daemon-data/` | Stop the daemon, then delete the directory |
| Artifacts (plans, patches, screenshots, observations) | `.colleague/daemon-data/.colleague/artifacts/` | Delete the meeting subdirectory, or the artifacts tree |
| Hosted pairing hashes | `.colleague/daemon-data/hosted/runner-state.json` | Portal/CLI/SDK **Unpair**, or delete the file after stopping the daemon |
| CLI last meeting id | `.colleague/cli-meeting.json` | Delete the file |
| Portal active meeting | `.colleague/portal-active.json` | Stop the colleague from the portal |
| API keys / meeting invite | `.env`, `.env.meeting` | Edit or delete; never commit |

Screenshots from incoming shared-content capture are stored as artifacts (`kind: screenshot` / `observation`), not as a separate public dump. Pairing codes and `deviceEnrollment` are shown once and are not written to these files.

Restarting the meeting participant starts a **new** GPT-Live voice context even when a coding-agent session can be resumed.
