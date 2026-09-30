# Third-party source

This repository includes source snapshots, not nested Git repositories. Their original license files are retained.

| Directory | Upstream | Commit | License |
| --- | --- | --- | --- |
| `joinly/` | https://github.com/joinly-ai/joinly | `4ea1259de329aa6965d9fdb806db64a7cdeeb683` | MIT; see `joinly/LICENSE` |

`joinly/` is a subset of that revision: the Playwright browser session, the PulseAudio and Xvfb virtual devices, the camera feed, and the Microsoft Teams and Google Meet controllers, with the types and settings they use. Joinly's speech models (Whisper, Silero, Kokoro), its Deepgram, ElevenLabs, and Google speech services, its server, and its client were removed, and the kept modules were trimmed to what Colleague AI imports. See [joinly/README.md](joinly/README.md). All three platforms use the browser session and virtual devices; Zoom has its own controls in `meeting-runtime/` and uses no Joinly controller.

Changes to the kept Joinly modules: persistent browser-profile support, browser cleanup, optional guest-name entry for signed-in Google sessions, a longer join-state wait, and explicit guest-rejection diagnostics.

The meeting image (`Dockerfile.meeting`) installs Playwright's Chromium, PulseAudio, Xvfb, x11vnc, noVNC, and websockify from their publishers; their licenses remain those of their respective publishers. No model weights are downloaded.

The local control panel uses Mozilla PDF.js (`pdfjs-dist`, Apache-2.0) to extract PDF text and Mammoth (`mammoth`, BSD-2-Clause) to extract DOCX text. Their notices and dependency licenses are included in the installed npm packages and lockfile.
