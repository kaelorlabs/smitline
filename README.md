# Colleague AI

**An AI teammate you can talk to while the work happens.**

A hackathon prototype for bringing an agent into a live conversation. Join a local voice room, speak, and hear a contextual reply. Built for the Agents, Everywhere hackathon.

## Current checkpoint

- Browser microphone capture with automatic end-of-speech detection.
- Local Whisper transcription and Kokoro speech synthesis, reusing Joinly's speech stack.
- Replies from the signed-in Codex CLI with recent conversation context.
- Live transcript, microphone mute, stop playback, and leave-call controls.
- WebRTC signaling and peer audio for additional local tabs; multi-person audio is implemented but not yet verified end to end.

The single-participant microphone-to-spoken-reply loop has been observed working. **Response latency is currently too high for natural conversation.** This commit preserves the working baseline before optimizing it.

## Run the live room

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

## Next: natural conversation

1. Measure end-of-speech, transcription, agent startup, first response, and first audio latency separately.
2. Keep a backend agent session warm instead of launching a CLI process for every utterance.
3. Stream replies into speech synthesis and playback as they arrive.
4. Add interruption handling and improve voice-activity detection.
5. Verify a three-person call, then add the research and sales-chart demo.

## Source and attribution

See [THIRD_PARTY.md](THIRD_PARTY.md) for pinned upstream snapshots and [SUBMISSION.md](SUBMISSION.md) for the division between inherited infrastructure and hackathon work. Runtime recordings, transcripts, logs, queues, and browser sessions are excluded from Git.
