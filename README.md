# Colleague AI

**An AI teammate you can talk to while the work happens.**

A hackathon prototype for bringing an agent into a live conversation. Join a local voice room, speak, and hear a contextual reply. Built for the Agents, Everywhere hackathon.

## Current checkpoint

The GPT-Live 1 API call is working: a user joined, spoke, and heard the agent respond. The browser streams microphone audio directly to OpenAI over WebRTC and displays live captions. A small local Node.js server creates the session while keeping the API key out of browser code.

### Run GPT-Live 1

Requires Node.js 22.6+ and an OpenAI project API key with GPT-Live access. No npm dependencies, Docker models, or Codex worker are needed for this call.

```bash
git clone https://github.com/ankitluthra/colleague-ai.git
cd colleague-ai
cp .env.example .env
chmod 600 .env
# Set OPENAI_API_KEY in .env using your editor.
bash start-gpt-live.sh
```

Open **http://127.0.0.1:8093/** and click **Join GPT-Live call**. Allow microphone access and use headphones. Mute and End call controls are available once connected. End the call before stopping the server with Ctrl-C. The `.env` file is ignored by Git; never commit an actual API key.

Audio is sent to OpenAI and API charges apply while connected. The session uses `gpt-live-1` for voice and configures `gpt-5.6-terra` for delegated reasoning, with no external tools. Session recording storage is disabled. The standalone browser call and the Zoom meeting agent have both been confirmed working by the user. End-to-end interruption behavior and delegated reasoning have not yet been separately verified.

Implementation: [server](gpt-live/server.mjs), [browser client](gpt-live/client.js), and [call page](gpt-live/index.html). Protocol reference: [OpenAI GPT-Live WebRTC guide](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live).

Run gateway checks without using an API key or making paid calls:

```bash
node --test gpt-live/server.test.mjs
```

## Join a Zoom meeting with web search and Codex

The Zoom bridge streams meeting audio to GPT-Live 1 and sends speech back through a virtual microphone. The user confirmed the Zoom voice call and local Tavily search working. Its GPT-5.6 Terra backend has two local functions: `search_web(query)` for current facts, and `run_codex(task, model)` for read-only technical work through the signed-in host Codex CLI. There are no local transcription or speech-generation models in this audio path.

With Docker running, set `OPENAI_API_KEY` and `TAVILY_API_KEY` in the ignored `.env` file:

```bash
cp zoom-live/meeting.env.example .env.zoom
chmod 600 .env.zoom
# Edit .env.zoom with the meeting URL and passcode.
bash start-zoom-live.sh
```

The launcher checks the host Codex login, starts the Zoom participant, and then keeps a Codex tool worker in the foreground. Codex credentials stay on the host. Tool jobs and results are exchanged through ignored local files and removed after each call. The first Codex call creates a persistent thread; later calls in the same Zoom meeting resume that thread, including after a worker restart. Different Zoom meetings use separate threads. Available model selections are `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra` (the balanced default), `gpt-5.6-luna`, and `gpt-5.5`. The chosen model is applied to each turn. Codex runs in a read-only sandbox inside `zoom-live/codex-workspace`.

The participant is named **Colleague AI**. It joins with its Zoom microphone muted and keeps listening. Unmute its microphone in the agent browser viewer to allow replies; muting it again discards pending speech. As host, open Participants, hover over Colleague AI, and select Ask to Unmute. The agent accepts the host-request dialog automatically; it does not click its toolbar Unmute button on its own. Even while unmuted, the voice prompt instructs the agent to stay silent until directly addressed with **“Hey colleague”**. It answers that request (including necessary clarifications and search results), then returns to quiet listening. This is model-prompt behavior, not a deterministic wake-word detector. The status endpoint reports `muted` and `listening`. Admit it if the host uses a waiting room. The [local browser viewer](http://127.0.0.1:6082/vnc.html?autoconnect=true) shows its Zoom browser. Human verification, sign-in, or terms acceptance can require user interaction.

After unmuting, try saying: “Hey colleague, ask Codex using GPT-5.6 Terra to design a Python retry helper,” or “Hey colleague, search the web for OpenAI's GPT-Live documentation.” The [local status endpoint](http://127.0.0.1:8094/health) reports tool starts/completions, the selected Codex model, backend status, source links, and recent captions held in memory. These captions and Codex tasks may contain meeting content; the endpoint is bound to localhost. Meeting audio is sent to OpenAI; voice, backend, and Codex usage are billed by OpenAI; local search calls use your Tavily account. Only the function query is sent to Tavily.

Stop the agent and leave the meeting:

```bash
docker compose -f compose.zoom.yaml stop zoom-live
```

Configuration is loaded at startup. After editing code or environment values, use `docker compose -f compose.zoom.yaml up -d --force-recreate zoom-live` for another run. The supplied meeting invite and API key remain in ignored local files.

## Earlier local-model checkpoint

- Browser microphone capture with automatic end-of-speech detection.
- Local Whisper transcription and Kokoro speech synthesis, reusing Joinly's speech stack.
- Replies from the signed-in Codex CLI with recent conversation context.
- Live transcript, microphone mute, stop playback, and leave-call controls.
- WebRTC signaling and peer audio for additional local tabs; multi-person audio is implemented but not yet verified end to end.

The single-participant microphone-to-spoken-reply loop has been observed working. **Response latency is currently too high for natural conversation.** This commit preserves the working baseline before optimizing it.

## Run the earlier local-model room

For agent-assisted setup, use the repository's [setup-colleague-ai skill](.agents/skills/setup-colleague-ai/SKILL.md). Invoke `$setup-colleague-ai` in an agent that discovers this repository's skills, or ask it to read that file. It covers a fresh clone, Docker build, Codex login, live voice verification, and troubleshooting.

Prerequisites: Docker with Compose, Python 3.10+, and an authenticated Codex CLI. On macOS the launcher also recognizes the CLI bundled inside `/Applications/ChatGPT.app`. Set `CODEX_BIN` if yours lives elsewhere.

```bash
git clone https://github.com/ankitluthra/colleague-ai.git
cd colleague-ai
codex login
bash start-live.sh
```

Open **http://127.0.0.1:8092/**, click **Join live call**, and allow microphone access. Keep the terminal running for agent replies. Use headphones and pause after a sentence. Initial Docker setup downloads Chromium and the local speech models and can take several minutes.

The agent uses your Codex account and its usage allowance. Audio processing stays local; microphone transcripts and recent room conversation are sent to OpenAI through Codex for replies. The reply worker uses a read-only sandbox. It currently produces conversational answers; research, database actions, and chart generation are future work.

Only localhost ports are published. This is a local demo on one computer, not a hosted conferencing product. Stop the worker with Ctrl-C, then stop the web service with:

```bash
docker compose --profile live stop live
```

## Architecture

Browser microphone → utterance recording → Whisper → local job queue → Codex CLI → Kokoro → browser audio.

The room server runs in Docker. The Codex worker runs on the host so it can use the user's existing CLI login. A mounted job directory connects them. Additional browser tabs exchange peer audio through WebRTC using the room's WebSocket signaling.

## Other demos

### Recorded mock meeting

```bash
docker compose build joinly
docker compose --profile mock up -d --build mock
```

Open http://127.0.0.1:8090/result for status and measured transcription results. The fixture plays Joinly's bundled 36-second recording through Chromium and PulseAudio. The observed run achieved a 15.1% word error rate against the reference, within the upstream 20% threshold. This is not a live call. Replay with `docker compose --profile mock restart mock`.

### Joinly MCP server

```bash
docker compose up -d --build joinly
curl http://127.0.0.1:8000/health
docker compose exec -T joinly /app/.venv/bin/python /demo/smoke.py
```

The MCP endpoint is http://127.0.0.1:8000/mcp/. The smoke test checks tool discovery and local speech generation/transcription.

Google Meet integration is experimental. Guest admission was rejected in the test meeting, and signed-in attempts encountered participant-selector issues. The optional `login` Docker profile provides a local browser viewer, but external-call support is not a verified part of this checkpoint. No Google profile or credentials are included.

## Next: meeting integration

1. Measure GPT-Live response latency and verify interruptions during real conversation.
2. Broaden Zoom testing across multiple speakers, waiting rooms, and meeting interruptions.
3. Add backend research and sales-chart tools, with visible task progress.

## Source and attribution

See [THIRD_PARTY.md](THIRD_PARTY.md) for pinned upstream snapshots and [SUBMISSION.md](SUBMISSION.md) for the division between inherited infrastructure and hackathon work. Runtime recordings, transcripts, logs, queues, and browser sessions are excluded from Git.
