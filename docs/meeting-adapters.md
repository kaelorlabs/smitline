# Meeting adapters

Colleague AI runs Zoom and Teams through a shared local browser/audio runtime. Paste a supported invitation into the console; platform detection is automatic. Google Meet and government Teams are not enabled.

## Operation

1. Run `./start-control-panel.sh` and open http://127.0.0.1:8095.
2. Paste an HTTPS Zoom, Teams commercial, or Teams Free meeting URL.
3. For Teams meetings that require an account, choose **Connect Microsoft account**, then **Open meeting view**. Complete Microsoft sign-in yourself. Once the console reports account connected, stop the account browser. The browser profile is stored locally in the ignored `meeting-runtime/profiles/` directory.
4. Start the colleague. Teams tries an isolated guest browser first and retries with the connected profile only when guest access is explicitly denied. Tenant policy may still refuse admission.
5. Admit the participant. It connects computer audio and starts listening continuously.
6. The adapter opens the platform microphone once and keeps the audio connection stable. The virtual microphone transmits silence while Colleague AI listens and immediately transports speech when GPT-Live chooses to respond.
7. GPT-Live owns conversational turn-taking, including pauses, backchannels, and interruptions. The local runtime does not classify participant speech or impose an additional silence delay.
8. A host or participant mute is authoritative; Colleague AI does not reopen the platform microphone automatically.
9. Stop the colleague to finalize its local transcript. To remove the Microsoft session, stop the browser and choose **Disconnect**. This removes the local profile; it does not revoke the account's sessions on other devices.

A connected profile marker means a signed-in Teams account menu was seen. Microsoft can expire that session. If fallback asks for sign-in again, reconnect through the console. One profile and one meeting/account browser can run at a time. Signed-in participation can display the Microsoft account's name rather than the configured guest name.

## Voice context and usage

The runtime keeps **one continuous `gpt-live-1` session** from admission to shutdown, preserving the original audio conversation within the model's context limits. Its live instructions default to silence and permit a response only for a direct address, explicit question or task, requested tool result, or an important factual correction that can be established.

**Quiet participation is not zero API usage.** Live has no Realtime `create_response: false` control. Prompted selectivity reduces unnecessary responses but does not guarantee that the service never generates one. The tool policy limits delegation to explicit actionable requests. Work already submitted to an external tool can finish after the conversation changes.

The health record exposes `generated_audio_bytes`, `discarded_audio_bytes`, and `output_bytes`. These measure application audio handling, not a billing estimate. `usage_seconds` is the provider's cumulative Live usage. No transcription-only replacement or voice model switch is implemented.

Reference: https://developers.openai.com/api/reference/resources/live/primary-websocket

## Adapter contract

`MeetingPlatformAdapter` owns joining, admission/authentication states, audio connection, microphone controls, chat, leave, termination detection, and capabilities. `adapters.REGISTRY` maps recognized platforms to implementations; `meeting_urls` validates URLs before opening them. Zoom-specific DOM interactions remain in its adapter/helpers. Teams uses the Joinly Teams controller for chat and leave, with explicit admission and microphone checks in Colleague AI.

The bridge owns the GPT-Live connection, virtual devices, tool dispatch, transcripts, and selective participation prompt. Platform mute controls establish the audio connection and respect an external mute. A separate virtual gate transports model output without repeatedly clicking the meeting toolbar or interpreting meeting speech.

Text chat delivery is available through `send_meeting_chat` when requested by a participant. Submission is not proof of recipient delivery. Teams charts are saved locally. Zoom chart upload remains experimental. Neither adapter implements screen sharing in this release.

## Breaking migration

The current names are `meeting-runtime/`, `.env.meeting`, `MEETING_URL`, `MEETING_PASSCODE`, `compose.meeting.yaml`, Docker service `meeting-agent`, and `start-meeting-agent.sh`. Old Zoom-specific settings are not read. Move existing local jobs, workspace, context, and recordings with the runtime directory; do not delete them. Update local launch scripts and stop the previous runtime before starting the new one. Secret files and browser profiles stay ignored by Git.

## Verification

Run `node --test control-panel/*.test.mjs` and the runtime unittest suite inside the built meeting image. Browser fixtures use that image's Chromium with no meeting accounts or API credentials. They verify URL rejection, lobby versus admission, account requirements, and individual microphone controls. Participation tests verify model-audio transport, stable microphone connection, blocked microphones, and external mute handling.

Live Zoom/Teams calls remain necessary to validate actual tenant admission, signed-in fallback, two-way audio, chat submission, and restart. Browser fixture tests alone do not establish production compatibility.

### Leaving an empty Teams call

Teams may keep a call open after other participants leave. The runtime leaves on the first check where the Teams toolbar reports one participant (the agent itself), with no grace period. Checks run every three seconds. An unavailable count does not trigger departure. If Teams does not expose a supported count label, automatic empty-room detection is unavailable; use Stop Colleague. Explicit meeting-ended and removal detection remains active. Automatic departure closes the Live session and preserves the local transcript.
