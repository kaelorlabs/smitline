# Alternative demos

The [main README](../README.md) documents the Zoom product path. These entry points are useful for isolating voice setup or reproducing earlier work.

## Standalone GPT-Live browser call

Requires Node.js 22.6+ and an OpenAI project API key with access to the configured GPT-Live API. This path does not need Docker or the Codex worker and does not expose the Zoom tool integrations.

From the repository root, create `.env` from `.env.example` if it does not exist, and set `OPENAI_API_KEY`. Then run:

```bash
bash start-gpt-live.sh
```

Open http://127.0.0.1:8093/, click **Join GPT-Live call**, and allow microphone access. Audio streams to OpenAI over WebRTC. The Node.js gateway keeps the project key out of browser code. End the call before stopping the server.

## Earlier local-model voice room

This baseline uses local Whisper transcription, the host Codex reply worker, and local Kokoro speech. It processes complete utterances and is noticeably slower than the streaming voice path. It does not provide the Zoom agent’s search, database, or chart tools.

Requires Docker Compose, Python 3.10+, and an authenticated Codex CLI:

```bash
bash start-live.sh
```

Open http://127.0.0.1:8092/ and join the room. Keep the worker terminal running. Audio processing stays local; transcripts and recent conversation are sent through Codex for replies. Additional-tab peer audio is implemented but has not been verified end to end.

For the full baseline workflow, read the [setup skill](../.agents/skills/setup-colleague-ai/SKILL.md).

Stop the worker with Ctrl-C, then:

```bash
docker compose --profile live stop live
```

## Recorded mock meeting

```bash
docker compose build joinly
docker compose --profile mock up -d --build mock
```

Open http://127.0.0.1:8090/result. This plays a bundled recording through the browser/audio stack and reports transcription results. It is not a meeting you can join.

## Joinly MCP smoke test

```bash
docker compose up -d --build joinly
curl http://127.0.0.1:8000/health
docker compose exec -T joinly /app/.venv/bin/python /demo/smoke.py
```

The MCP endpoint is http://127.0.0.1:8000/mcp/. This checks the earlier Joinly tool and speech stack, not the GPT-Live Zoom integration.
