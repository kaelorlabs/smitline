# Contributing

Thanks for helping. Humans and coding agents follow the same rules.

1. **Start with [AGENTS.md](AGENTS.md).** It lists the docs to read first, the invariants every change keeps (GPT-Live is the only voice, brief in and result out, fail closed, local-first, always disclose), and how to run the tests.
2. **Run the tests** as AGENTS.md describes: the runtime suite inside the meeting image, `npm test`, and the Python SDK tests. Unit tests do not prove live call or meeting behavior; after changing browser, audio, or phone code, also try a real call or meeting and say so in the pull request.
3. **Keep pull requests focused.** One change per pull request, with the docs it affects updated in the same pull request.
4. **Never commit secrets or runtime state:** `.env`, `.smitline/`, recordings, browser profiles, transcripts, call records, or credentials. They are gitignored; keep them that way.

Report security problems privately; see [SECURITY.md](SECURITY.md).

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
