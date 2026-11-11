# Third-party source

This repository includes source snapshots, not nested Git repositories. Their original license files are retained.

| Directory | Upstream | Commit | License |
| --- | --- | --- | --- |
| `joinly/` | https://github.com/joinly-ai/joinly | `4ea1259de329aa6965d9fdb806db64a7cdeeb683` | MIT; see `joinly/LICENSE` |
| `meeting-runtime/hold_models/` (FireRedVAD's models) | https://github.com/FireRedTeam/FireRedVAD | `c30ec49e8cc69642b0ee65362eba11b9d11c6e54` | Apache-2.0, code and weights; see `meeting-runtime/hold_models/LICENSE-FireRedVAD` |
| `meeting-runtime/hold_models/silero_vad.onnx` | https://github.com/snakers4/silero-vad | tag `v6.2.3` (`5cd7945676eb32225748052e2e6a0580e4686a08`) | MIT; see `meeting-runtime/hold_models/LICENSE-silero-vad` |

`joinly/` is a subset of that revision: the Playwright browser session, the PulseAudio and Xvfb virtual devices, the camera feed, and the Microsoft Teams and Google Meet controllers, with the types and settings they use. Joinly's speech models (Whisper, Silero, Kokoro), its Deepgram, ElevenLabs, and Google speech services, its server, and its client were removed, and the kept modules were trimmed to what Smitline imports. See [joinly/README.md](joinly/README.md). All three platforms use the browser session and virtual devices; Zoom has its own controls in `meeting-runtime/` and uses no Joinly controller.

Changes to the kept Joinly modules: persistent browser-profile support, browser cleanup, optional guest-name entry for signed-in Google sessions, a longer join-state wait, and explicit guest-rejection diagnostics.

`meeting-runtime/hold_models/` holds model files copied unchanged: FireRedVAD's streaming voice activity model, its audio event model and their normalization statistics, and Silero VAD's model. `hold_listener.py` runs them on the CPU while a phone call waits on hold ([Waiting on hold](docs/phone.md#waiting-on-hold)). Its README lists each file's sha256.

## In the published images

The images redistribute these components unchanged. Each keeps its own license; the license files ship inside the image or the package.

| Component | Where | License |
| --- | --- | --- |
| Node.js 22 | `smitline` image, from `node:22-bookworm-slim` | MIT, with bundled dependencies under their own licenses |
| Docker CLI and the Docker Compose plugin | `smitline` image, from `docker:29.8.1-cli` | Apache-2.0 |
| cloudflared | `smitline` image, from `cloudflare/cloudflared:2026.9.3` | Apache-2.0 |
| Mammoth (`mammoth`) | `smitline` image, npm; extracts DOCX text in the console | BSD-2-Clause |
| Mozilla PDF.js (`pdfjs-dist`) | `smitline` image, npm; extracts PDF text in the console | Apache-2.0 |
| ONNX Runtime (`onnxruntime`) | `smitline` image, PyPI; runs the hold detectors | MIT |
| kaldi-native-fbank | `smitline` image, PyPI; the hold detectors' audio features | Apache-2.0 |
| Playwright for Python | meeting image, PyPI | Apache-2.0 |
| Chromium and ffmpeg, as built by Playwright | meeting image, `/opt/ms-playwright` | Chromium: BSD-3-Clause and the third-party licenses bundled with it; ffmpeg: LGPL-2.1 (`COPYING.LGPLv2.1` next to it) |
| noVNC 1.5.0 | meeting image, `/usr/share/novnc` | MPL-2.0 for the core library, other files as listed in its `LICENSE.txt` |
| websockify | meeting image, PyPI | LGPL-3.0 |

The npm dependencies of Mammoth and PDF.js are listed with their licenses in `package-lock.json` and ship in `node_modules`. The Python packages (aiohttp, numpy, and their dependencies) come from PyPI, and the Debian packages (tini, PulseAudio, Xvfb, x11vnc) from Debian bookworm, each under its publisher's license; Debian's copyright files are in `/usr/share/doc`. No model weights are downloaded: the hold detectors' models come with the code, in `meeting-runtime/hold_models/`.

## Fonts

The local console serves three variable fonts from `control-panel/fonts/` (in the repository and the `smitline` image): the Latin subsets from Fontsource 5.3.0 (`@fontsource-variable`), unchanged. Each is under the SIL Open Font License 1.1, whose text is next to it.

| Font | Use | License |
| --- | --- | --- |
| Fraunces | Headings and the name beside the logo | OFL-1.1; see `control-panel/fonts/Fraunces-OFL.txt` |
| Jost | Text | OFL-1.1; see `control-panel/fonts/Jost-OFL.txt` |
| Geist Mono | Machine values, such as telemetry and logs | OFL-1.1; see `control-panel/fonts/GeistMono-OFL.txt` |
