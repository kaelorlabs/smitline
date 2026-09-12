# Colleague AI — hackathon checkpoint

## Idea

Bring a working AI teammate into a conversation. People discuss a problem, the agent gathers context, responds aloud, and ultimately performs useful work while the discussion continues.

## Implemented during this project

Local voice-room UI, microphone utterance capture, WebSocket signaling, host-side Codex job worker, conversation context handling, local speech round trip, Docker Compose orchestration, verification scripts, and experimental Google browser-session adaptations.

## Reused

Joinly's browser/audio infrastructure and Kokoro service, Whisper and Kokoro models, the upstream recorded meeting fixture, and Codex CLI authentication and inference. The CopilotKit starter kit is included as reference source and has not yet been integrated into the live room.

## Demonstrated

Single-participant live speech was transcribed and received a contextual, synthesized reply. The upstream recorded mock passed its transcription threshold. The server also rejects concurrent utterances while processing a reply.

## Not yet demonstrated

Natural low-latency dialogue, reliable interruption, multi-person peer audio, reliable external meeting participation, live research, sales database queries, and chart presentation.

## Submission work remaining

- Reduce conversational latency.
- Demonstrate one useful action during the call.
- Record the two-minute demo video.
- Complete the event's public repository and social-post requirements before final submission.
