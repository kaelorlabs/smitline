# Third-party source

This repository includes source snapshots, not nested Git repositories. Their original license files are retained.

| Directory | Upstream | Commit | License |
| --- | --- | --- | --- |
| `joinly/` | https://github.com/joinly-ai/joinly | `4ea1259de329aa6965d9fdb806db64a7cdeeb683` | MIT; see `joinly/LICENSE` |

`joinly/` is a subset of that revision: the Playwright browser session, the PulseAudio and Xvfb virtual devices, the camera feed, and the Microsoft Teams and Google Meet controllers, with the types and settings they use. Joinly's speech models (Whisper, Silero, Kokoro), its Deepgram, ElevenLabs, and Google speech services, its server, and its client were removed, and the kept modules were trimmed to what Colleague AI imports. See [joinly/README.md](joinly/README.md). All three platforms use the browser session and virtual devices; Zoom has its own controls in `meeting-runtime/` and uses no Joinly controller.

Changes to the kept Joinly modules: persistent browser-profile support, browser cleanup, optional guest-name entry for signed-in Google sessions, a longer join-state wait, and explicit guest-rejection diagnostics.

## In the published images

The images redistribute these components unchanged. Each keeps its own license; the license files ship inside the image or the package.

| Component | Where | License |
| --- | --- | --- |
| Node.js 22 | `colleague` image, from `node:22-bookworm-slim` | MIT, with bundled dependencies under their own licenses |
| Docker CLI and the Docker Compose plugin | `colleague` image, from `docker:29.8.1-cli` | Apache-2.0 |
| cloudflared | `colleague` image, from `cloudflare/cloudflared:2026.9.3` | Apache-2.0 |
| Mammoth (`mammoth`) | `colleague` image, npm; extracts DOCX text in the console | BSD-2-Clause |
| Mozilla PDF.js (`pdfjs-dist`) | `colleague` image, npm; extracts PDF text in the console | Apache-2.0 |
| Playwright for Python | meeting image, PyPI | Apache-2.0 |
| Chromium and ffmpeg, as built by Playwright | meeting image, `/opt/ms-playwright` | Chromium: BSD-3-Clause and the third-party licenses bundled with it; ffmpeg: LGPL-2.1 (`COPYING.LGPLv2.1` next to it) |
| noVNC 1.5.0 | meeting image, `/usr/share/novnc` | MPL-2.0 for the core library, other files as listed in its `LICENSE.txt` |
| websockify | meeting image, PyPI | LGPL-3.0 |

The npm dependencies of Mammoth and PDF.js are listed with their licenses in `package-lock.json` and ship in `node_modules`. The Python packages (aiohttp, numpy, and their dependencies) come from PyPI, and the Debian packages (tini, PulseAudio, Xvfb, x11vnc) from Debian bookworm, each under its publisher's license; Debian's copyright files are in `/usr/share/doc`. No model weights are downloaded.
