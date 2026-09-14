# Colleague AI local control panel

The local control panel is the main operator interface for Colleague AI. It keeps meeting configuration, tool selection, supplied reference material, runtime status, and transcripts in one place.

## Before the first run

Install the local requirements from the repository root:

```bash
cp .env.example .env
cp zoom-live/meeting.env.example .env.zoom
chmod 600 .env .env.zoom
npm install
```

Add `OPENAI_API_KEY` to `.env`. Add `TAVILY_API_KEY` when web search will be enabled. Install Docker Desktop, sign in to Codex with `codex login`, and confirm the login with `codex login status`.

The control panel never returns API keys to the browser. `.env`, `.env.zoom`, uploaded context, meeting transcripts, Codex job files, and generated artifacts are ignored by Git.

## Start the console

```bash
bash start-control-panel.sh
```

Open [http://127.0.0.1:8095](http://127.0.0.1:8095). Keep the terminal open while operating the meeting agent.

## Configure a meeting

1. Paste the complete Zoom invite URL. A URL containing Zoom's `pwd` parameter is supported.
2. Enter a passcode when Zoom requires one separately.
3. Choose the participant name and the default Codex model.
4. Enable only the tools needed for the meeting:
   - **Web search** uses the local `search_web` function backed by Tavily.
   - **Codex** runs read-only technical work in the selected workspace and maintains a session for the meeting.
   - **Charts** allows Codex results to be rendered locally and requires Codex.
5. Optionally select an absolute workspace path. The Codex worker can inspect only this workspace. When it is blank, the worker uses the empty, ignored `zoom-live/codex-workspace` directory.
6. Optionally add short meeting instructions. Use these for the agent's role, terminology, response style, or meeting-specific boundaries. Do not use them for credentials.
7. Run the checks, resolve any reported issue, and start the colleague.

The checks validate the meeting settings, required credentials, Docker availability, the selected workspace, and Codex login when that tool is enabled.

## Supply private reference context

Paste text or upload TXT, Markdown, CSV, TSV, JSON, YAML, PDF, or DOCX files from the context section. The console extracts text locally and stores it in `zoom-live/context/index.json`. Individual files are limited to 8 MB, a batch can contain up to 10 files, and all saved extracted text is limited to 1,000,000 characters.

The meeting agent receives a `search_context` tool only when saved sources exist at voice-session startup. It retrieves matching passages and names the source document in its answer. Restart the meeting participant after adding or clearing context so the voice session receives the correct tool list.

Reference context is suited to supplied facts, policies, plans, product notes, customer information, tables, and company terminology. Use the Codex workspace for deeper analysis of structured data, code, or a larger project. Uploaded documents remain local unless their relevant contents are included in a model request while answering a meeting question.

Use **Clear saved context** when the material should no longer be available. This deletes the local context index; it does not alter the original documents.

## Operate the call

The console reports the join stage, microphone state, listening state, selected tools, runtime log, and recorded sessions. Admit **Colleague AI** from the Zoom waiting room when prompted. The participant starts muted and continues listening. Use Zoom's **Ask to Unmute** action when it should be allowed to speak.

Stop the colleague from the console before starting another meeting or changing configuration. Starting a new voice session clears the voice model's conversation memory. The Codex tool can resume its meeting-scoped session when the meeting identity and workspace are unchanged.

## Review local records

Each call creates a directory under `zoom-live/recordings/` containing the incremental transcript and event trace. The console lists recent sessions and can open their transcripts. Generated charts and supporting data are stored with the call record.

The transcript records recognized and generated text. It is not a raw-audio archive and may include a generated response that was muted or interrupted before participants heard it.

## Command-line operation

The lower-level launcher remains available for automation and debugging:

```bash
bash start-zoom-live.sh
```

It reads `.env.zoom`, validates the configuration, builds and starts the Docker participant, and runs the host Codex worker when enabled. Runtime status is available at [http://127.0.0.1:8094/health](http://127.0.0.1:8094/health), and the browser viewer is available at [http://127.0.0.1:6082/vnc.html?autoconnect=true](http://127.0.0.1:6082/vnc.html?autoconnect=true).

## Troubleshooting

- If the console cannot start, run `npm install` and confirm Node.js 22 or later is active.
- If preflight reports Docker unavailable, start Docker Desktop and rerun the checks.
- If Codex is unavailable, run `codex login` or set `CODEX_BIN` to the executable path.
- If the participant is silent, admit it, use **Ask to Unmute**, and inspect the runtime log and health state.
- If Zoom does not admit the participant, inspect the browser viewer for a waiting-room, sign-in, passcode, or host-removal message.
- If supplied context is not available, add the source before starting the meeting or restart the participant after changing sources.
- If chart delivery fails, inspect the saved artifact in the call record. Direct Zoom chat attachment depends on host settings and Zoom web-client support.
