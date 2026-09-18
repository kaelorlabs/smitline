# Third-party source

This repository includes source snapshots, not nested Git repositories. Their original license files are retained.

| Directory | Upstream | Commit | License |
| --- | --- | --- | --- |
| `joinly/` | https://github.com/joinly-ai/joinly | `4ea1259de329aa6965d9fdb806db64a7cdeeb683` | MIT; see `joinly/LICENSE` |

Joinly modifications in this checkpoint: persistent browser-profile support, browser cleanup, optional guest-name entry for signed-in Google sessions, longer join-state wait, and explicit guest-rejection diagnostics.

Model weights are downloaded by the upstream Docker build; model and dependency licenses remain those of their respective publishers.

The local control panel uses Mozilla PDF.js (`pdfjs-dist`, Apache-2.0) to extract PDF text and Mammoth (`mammoth`, BSD-2-Clause) to extract DOCX text. Their notices and dependency licenses are included in the installed npm packages and lockfile.
