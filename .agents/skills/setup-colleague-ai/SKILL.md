---
name: setup-colleague-ai
description: Set up or resume Colleague AI's local Docker voice call with Whisper, the host Codex reply worker, and Kokoro speech. Use to reproduce the working hackathon demo or diagnose its startup.
---

# Set up Colleague AI

Reproduce the working local microphone → Whisper transcription → Codex reply → Kokoro spoken response baseline. Repository: https://github.com/ankitluthra/colleague-ai. The baseline was captured at commit `8388443`; use current main normally, or that checkpoint in a separate checkout when explicitly asked to reproduce the historical code exactly.

This is a local voice room at http://127.0.0.1:8092/, without a Google Meet or Zoom dependency. It processes complete utterances and is noticeably slow. Setup does not implement streaming, interruption, research tools, database queries, or charts. The vendored Agents Everywhere starter is available for later development and is not part of the running voice pipeline.

## Locate the project and prerequisites

Reuse an existing checkout containing `compose.yaml`, `start-live.sh`, `live/server.py`, and `live/worker.py`. Resolve paths from that checkout rather than assuming its folder name. On the original machine, the published snapshot folder was named `roommate-live`, while the running demo used `meeting-agent-demo`.

For a fresh installation in the user's chosen parent directory:

```bash
git clone https://github.com/ankitluthra/colleague-ai.git
cd colleague-ai
```

The repository already vendors both upstream source trees and local Joinly patches. Do not replace them with fresh upstream clones. Consult `THIRD_PARTY.md` for revisions and licenses. A public clone needs no GitHub login.

Check for Git, host Python 3.10+, Docker with Compose, and an authenticated Codex CLI. The worker imports `fcntl`, so use macOS or Linux (including an appropriate WSL environment), not native Windows Python.

```bash
python3 --version
docker info
docker compose version
docker compose config --quiet
```

If Docker Desktop is installed but stopped, start it and wait for `docker info` to succeed. The image build downloads speech models and may take several minutes. It needs network access and sufficient disk space.

The worker resolves Codex from `CODEX_BIN`, then PATH, then the macOS fallback `/Applications/ChatGPT.app/Contents/Resources/codex`. For that fallback, when present:

```bash
export CODEX_BIN=/Applications/ChatGPT.app/Contents/Resources/codex
"$CODEX_BIN" login status
```

Otherwise use `codex login status`, or the user's existing `CODEX_BIN`. If login is required, let the user complete the CLI's interactive `login` flow. Preserve existing authentication. No separate OpenAI API key is needed for this baseline.

Audio transcription and speech generation run locally. The worker sends transcribed text and recent conversation to OpenAI through the signed-in Codex account, using that account's allowance. Reuse authorization already given in the current conversation. If it has not been given, explain this data flow and obtain authorization before starting the reply worker; the skill itself does not grant consent.

## Start or resume the live room

First check `docker compose ls`, `docker compose --profile live ps`, and whether port 8092 already serves `/health`. If an existing instance is running, inspect its container mounts or Compose working-directory label to identify its checkout and reuse it. Starting another copy on the same port will fail; do not stop an active call simply to change folders.

For a fresh start, run from the checkout in a terminal/session that remains alive:

```bash
bash start-live.sh
```

The script creates `live/jobs` and `live/workspace`, builds `meeting-agent-joinly:local` from the vendored Dockerfile, starts the Compose `live` service, and runs `python3 -u live/worker.py` on the host. The host and container exchange jobs through the mounted `live` directory. Keep the worker terminal running; record its session handle when using agent tools.

Only the `live` service is required. Building the `joinly` image does not require running the separate MCP service. Keep Compose's localhost port bindings.

## Verify the actual voice demo

In another terminal:

```bash
docker compose --profile live ps
curl --fail http://127.0.0.1:8092/health
```

After model initialization, expect `status: "ready"` and `worker_connected: true`. `agent_status` reports the current turn stage. The worker heartbeat can remain apparently connected for up to 130 seconds after a stopped worker, so this is a startup check, not proof of a working answer.

Open http://127.0.0.1:8092/ on the same computer. Click **Join live call**, grant microphone access, and enable automatic replies if needed. Use headphones to avoid feeding synthesized speech back into the microphone. Have the user speak a short sentence, then pause.

Verify a microphone transcript, an assistant reply, and audible playback. Observe the stages Transcribing → Thinking → Preparing voice → Ready. Report which checks were actually performed; page loading or health alone does not establish voice success. Do not send synthetic test utterances into an occupied call.

The original baseline passed a real user voice exchange, but natural conversational latency remained unfinished. Browser silence detection waits roughly 1.1 seconds, transcription is batched, a fresh Codex process handles each turn, and the server collects TTS output before playback. Additional-tab WebRTC signalling exists; multi-user audio was not verified end to end. This is not a remotely hosted meeting service.

## Diagnose concrete failures

- **Page unavailable:** inspect `docker compose --profile live logs --tail 100 live`; distinguish initial model loading from a failed container. Build the full base image rather than substituting an image without the bundled speech models.
- **Worker disconnected or answers fail:** inspect the host worker terminal, selected Codex executable, login status, and usage availability. The live container alone cannot produce Codex replies.
- **Worker lock error:** another worker holds `live/jobs/worker.lock`. Identify and reuse that process; removing the lock file can allow competing workers.
- **HTTP 409:** one turn is already processing. Wait for Ready before another utterance. This baseline intentionally serializes turns.
- **No transcript:** check browser microphone permission and selected input, unmute, and speak closer to the microphone. Empty/no-speech input may return 422.
- **Port collision:** identify the existing Compose project and source mount before choosing to reuse it or stop it with the user's task context. Avoid changing the port in only one component.

Runtime jobs can contain conversation text. Keep jobs, recordings, logs, credentials, and browser profiles out of commits; preserve the repository's ignore rules.

## Optional earlier demos

Run these only when requested or useful for isolating a failure. They are not prerequisites for the live room.

Recorded mock meeting (fixture playback, not a human-joinable call):

```bash
docker compose build joinly
docker compose --profile mock up -d --build mock
```

Open http://127.0.0.1:8090/result. The original fixture test measured approximately 15% word error rate; do not report that result as a new test without rerunning it.

Joinly MCP and local speech smoke test:

```bash
docker compose up -d --build joinly
curl --fail http://127.0.0.1:8000/health
docker compose exec -T joinly /app/.venv/bin/python /demo/smoke.py
```

Google Meet login support remains experimental. Anonymous joining was rejected; signing in did not establish a successful external meeting demo. Do not require Google credentials to reproduce the local room. Its saved browser volume is private runtime state and is not distributed with the repository.

## Stop

Stop the worker started for this setup with Ctrl-C, then from its checkout:

```bash
docker compose --profile live stop live
```

Leave unrelated services and saved browser volumes intact. Report the checkout, local URL, startup/voice verification results, and any remaining blocker.
