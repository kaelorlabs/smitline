# Meeting adapters

Colleague AI runs Zoom, Microsoft Teams, and Google Meet through a shared browser/audio runtime in one Docker container (`colleague-meeting:local`). An agent joins with `start_call` on the `meeting` channel and the invite URL as `to` (see [calls](calls.md)); you can also paste the invitation into the console. Platform detection is automatic. Government Teams is not enabled.

## Operation

The steps below use the console. When an agent starts the meeting, steps 1 and 2 happen through the calls API, and the rest is the same.

1. Open http://127.0.0.1:8095 (from a checkout, run `./start-control-panel.sh` first).
2. Paste an HTTPS Zoom, Teams commercial, Teams Free, or Google Meet (`https://meet.google.com/xxx-yyyy-zzz`) meeting URL.
3. For Teams or Meet meetings that require an account, choose **Connect Microsoft account** or **Connect Google account**, then **Open meeting view**. Complete sign-in yourself on the official page. Once the console reports account connected, stop the account browser. Browser profiles are stored locally in the ignored `meeting-runtime/profiles/` directory.
4. Start the colleague. The adapter tries an isolated guest browser first and retries with the connected profile only when guest access is explicitly denied. Tenant or host policy may still refuse admission.
5. Admit the participant. It connects computer audio and starts listening continuously.
6. The adapter opens the platform microphone once and keeps the audio connection stable. The virtual microphone transmits silence while Colleague AI listens and immediately transports speech when GPT-Live chooses to respond.
7. Once its microphone is open, Colleague AI says one short AI disclosure naming the person it acts for, then listens. Set `COLLEAGUE_MEETING_INTRO=0` in `.env` to turn this off. `COLLEAGUE_VOICE` picks the GPT-Live voice.
8. GPT-Live owns conversational turn-taking, including pauses, backchannels, and interruptions. The local runtime does not classify participant speech or impose an additional silence delay.
9. A host or participant mute is authoritative; Colleague AI does not reopen the platform microphone on its own. In Zoom it accepts the host's explicit "Ask to unmute" request.
10. Stop the colleague to finalize its local transcript. To remove a Microsoft or Google session, stop the browser and choose **Disconnect**. This removes the local profile; it does not revoke the account's sessions on other devices.

A connected profile marker means a signed-in account menu was seen. Google or Microsoft can expire that session. If fallback asks for sign-in again, reconnect through the console. One profile and one meeting/account browser can run at a time. Signed-in participation can display the account's name rather than the configured guest name.

## Voice context and usage

The runtime keeps **one continuous `gpt-live-1` session** from admission to shutdown, preserving the original audio conversation within the model's context limits. Its live instructions default to silence and permit a response only for a direct address, an explicit question or task, or an important factual correction that can be established. Questions that need careful reasoning or precise facts go to a backend model through Responses delegation (`COLLEAGUE_MEETING_BACKEND_MODEL`, default `gpt-5.6-terra`; `COLLEAGUE_MEETING_WEB_SEARCH=1` adds OpenAI web search). The backend gets the meeting context and guidance.

**Quiet participation is not zero API usage.** Live has no Realtime `create_response: false` control. Prompted selectivity reduces unnecessary responses but does not guarantee that the service never generates one. A backend answer already requested can arrive after the conversation has moved on.

The health record exposes `generated_audio_bytes`, `discarded_audio_bytes`, and `output_bytes`. These measure application audio handling, not a billing estimate. `usage_seconds` is the provider's cumulative Live usage. No transcription-only replacement or voice model switch is implemented.

Reference: https://developers.openai.com/api/reference/resources/live/primary-websocket

## Adapter contract

`MeetingPlatformAdapter` owns joining, admission/authentication states, audio connection, microphone controls, chat, leave, termination detection, and capabilities. `adapters.REGISTRY` maps recognized platforms to implementations; `meeting_urls` validates URLs before opening them. Zoom-specific DOM interactions remain in its adapter/helpers. Teams and Google Meet reuse Joinly controllers for chat and leave, with explicit admission and microphone checks in Colleague AI. Signed-in fallback is declared on the adapter (`signed_in_profile`); the join path does not branch on platform id.

The bridge (`meeting-runtime/bridge.py`) owns the GPT-Live connection, Responses delegation, virtual devices, transcripts, and the selective participation prompt. Platform mute controls establish the audio connection and respect an external mute; `accept_unmute_request` lets an adapter follow a host's explicit request to unmute (Zoom only today). A separate virtual gate transports model output without repeatedly clicking the meeting toolbar or interpreting meeting speech.

Adapters do not share a screen and do not read shared screens.

## Troubleshooting

- **Colleague AI never speaks in Zoom, and `microphoneState` is `blocked`.** The host has turned off "Allow participants to unmute themselves", so the one unmute at session start failed. The host can click **Ask to unmute** on Colleague AI's video tile (or in the participant list); the Zoom adapter accepts "The host would like you to unmute" and arms its microphone. It then stays unmuted between replies, so the host asks only once unless they mute it again.
- **A host muted Colleague AI.** It stays muted. Unmute it from the meeting UI, or in Zoom click **Ask to unmute** again.

## Breaking migration

The current names are `meeting-runtime/`, `.env.meeting`, `MEETING_URL`, `MEETING_PASSCODE`, `compose.meeting.yaml`, Docker service `meeting-agent`, and `start-meeting-agent.sh`. Old Zoom-specific settings are not read. Move existing local context, recordings, and profiles with the runtime directory; do not delete them. Update local launch scripts and stop the previous runtime before starting the new one. Secret files and browser profiles stay ignored by Git.

## Verification

Run `node --test control-panel/*.test.mjs` and the runtime unittest suite inside the built meeting image (the command is in [AGENTS.md](../AGENTS.md#tests)). Browser fixtures use that image's Chromium with no meeting accounts or API credentials. They verify URL rejection, lobby versus admission, account requirements, and individual microphone controls. Participation tests verify model-audio transport, stable microphone connection, blocked microphones, and external mute handling.

See [capabilities](capabilities.md) for the adapter matrix and [architecture](architecture.md#guest-then-signed-in-fallback) for the guest-then-signed-in sequence. Live Zoom/Teams/Meet calls remain necessary to validate actual tenant admission, signed-in fallback, two-way audio, chat submission, and restart. Browser fixture tests alone do not establish production compatibility.

### Leaving an empty Teams or Meet call

Teams and Google Meet may keep a call open after other participants leave. The runtime leaves on the first check where the in-meeting toolbar reports one participant (the agent itself), with no grace period. Checks run every three seconds. An unavailable count does not trigger departure. If the platform does not expose a supported count label, automatic empty-room detection is unavailable; use Stop Colleague. Zoom has no participant-count API in this runtime, so it relies on ended/removal detection only. Explicit meeting-ended and removal detection remains active on all three platforms. Automatic departure closes the Live session and preserves the local transcript.
